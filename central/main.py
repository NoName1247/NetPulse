import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
import httpx
import sentry_sdk
import structlog
from fastapi import FastAPI, Header, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from central.alerts import TelegramAlerter
from central.analytics import AnalyticsService
from central.config import Settings, get_settings
from central.db import Database
from central.diagnostics import DiagnosticsService, TurnstileVerifier
from central.geo import GeoIPService
from central.logging_setup import configure_logging, configure_sentry
from central.models import (
    ActiveIncidentsResponse,
    AppError,
    BadRequestError,
    HeatmapResponse,
    HeatmapWindow,
    RateLimitError,
    TestCompleteRequest,
    TestCompleteResponse,
    TestInitRequest,
    TestInitResponse,
)
from central.scheduler import BackgroundScheduler
from central.state import InMemoryState

logger = structlog.get_logger("central.main")


def _extract_client_ip(request: Request) -> str:
    """Извлекает IP из CF-Connecting-IP (за Caddy/Cloudflare), иначе client.host."""
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip and cf_ip.strip():
        return cf_ip.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first_hop = xff.split(",")[0].strip()
        if first_hop:
            return first_hop
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


def create_app(
    settings: Settings | None = None,
    *,
    db: Database | None = None,
    geo: GeoIPService | None = None,
    state: InMemoryState | None = None,
    alerter: TelegramAlerter | None = None,
    turnstile: TurnstileVerifier | None = None,
) -> FastAPI:
    cfg = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(cfg.environment)
        configure_sentry(cfg.sentry_dsn, cfg.environment)

        http_client = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=40, max_keepalive_connections=10)
        )

        owns_db = db is None
        db_inst = db or Database(
            cfg.database_url,
            min_size=cfg.db_pool_min_size,
            max_size=cfg.db_pool_max_size,
            command_timeout_s=cfg.db_command_timeout_s,
        )

        state_inst = state or InMemoryState(
            session_ttl_s=cfg.session_ttl_s,
            idempotency_ttl_s=cfg.idempotency_ttl_s,
            salt_cache_ttl_s=cfg.salt_cache_ttl_s,
            heatmap_cache_ttl_s=cfg.heatmap_cache_ttl_s,
            rate_limit_window_s=cfg.rate_limit_window_s,
            rate_limit_fixed_max=cfg.rate_limit_fixed_max,
            rate_limit_mobile_max=cfg.rate_limit_mobile_max,
            healthcheck_fail_threshold=cfg.healthcheck_fail_threshold,
            peering_alert_window_s=cfg.peering_alert_window_s,
        )

        owns_geo = geo is None
        geo_inst = geo or GeoIPService(cfg.geolite2_asn_path, cfg.geolite2_city_path)
        if owns_geo:
            geo_inst.load_databases()

        if owns_db:
            await db_inst.connect()
            if cfg.seed_districts_on_startup:
                await db_inst.seed_h3_districts()

        ref_data = await db_inst.load_reference_data()
        state_inst.ref_data = ref_data
        state_inst.node_registry.load_nodes(ref_data.nodes)

        alerter_inst = alerter or TelegramAlerter(
            cfg.telegram_bot_token,
            cfg.telegram_chat_id,
            cooldown_s=cfg.alert_cooldown_s,
            http_client=http_client,
        )
        turnstile_inst = turnstile or TurnstileVerifier(
            enabled=cfg.turnstile_enabled,
            secret_key=cfg.turnstile_secret_key,
            verify_url=cfg.turnstile_verify_url,
            timeout_s=cfg.turnstile_timeout_s,
            http_client=http_client,
        )
        analytics_inst = AnalyticsService(
            db=db_inst,
            state=state_inst,
            min_samples=cfg.heatmap_min_samples,
        )
        diagnostics_inst = DiagnosticsService(
            settings=cfg,
            db=db_inst,
            state=state_inst,
            geo=geo_inst,
            analytics=analytics_inst,
            alerter=alerter_inst,
            turnstile=turnstile_inst,
        )
        scheduler_inst = BackgroundScheduler(
            settings=cfg,
            db=db_inst,
            state=state_inst,
            geo=geo_inst,
            alerter=alerter_inst,
            http_client=http_client,
        )

        app.state.settings = cfg
        app.state.db = db_inst
        app.state.state = state_inst
        app.state.geo = geo_inst
        app.state.analytics = analytics_inst
        app.state.diagnostics = diagnostics_inst
        app.state.scheduler = scheduler_inst
        app.state.http_client = http_client

        if cfg.enable_scheduler:
            scheduler_inst.start()

        logger.info(
            "central_app_started",
            environment=cfg.environment,
            nodes=len(ref_data.nodes),
            carriers=len(ref_data.carriers_by_id),
            districts=len(ref_data.districts_by_h3),
        )
        try:
            yield
        finally:
            await scheduler_inst.stop()
            if owns_geo:
                geo_inst.close()
            await http_client.aclose()
            if owns_db:
                await db_inst.close()
            logger.info("central_app_stopped")

    app = FastAPI(
        title="NetPulse MY Central API",
        version="1.0.0",
        docs_url=None if cfg.environment == "production" else "/docs",
        redoc_url=None,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins_list(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-Turnstile-Token", "Idempotency-Key"],
    )

    if cfg.origin_secret:
        expected_secret = cfg.origin_secret

        @app.middleware("http")
        async def verify_origin_secret_middleware(request: Request, call_next):
            if request.url.path != "/healthz":
                provided = request.headers.get("x-origin-secret", "")
                if not hmac.compare_digest(provided, expected_secret):
                    return JSONResponse(
                        status_code=403,
                        content={
                            "error": {
                                "code": "FORBIDDEN_ORIGIN",
                                "message": "Direct origin access is forbidden.",
                            }
                        },
                    )
            return await call_next(request)

    @app.exception_handler(RateLimitError)
    async def rate_limit_handler(_request: Request, exc: RateLimitError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            headers={"Retry-After": str(exc.retry_after_s)},
            content={"error": {"code": exc.error_code, "message": exc.message}},
        )

    @app.exception_handler(AppError)
    async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.error_code, "message": exc.message}},
        )

    @app.exception_handler(asyncpg.PostgresError)
    async def postgres_error_handler(
        _request: Request, exc: asyncpg.PostgresError
    ) -> JSONResponse:
        sentry_sdk.capture_exception(exc)
        logger.error("database_query_failed", error_type=type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "DATABASE_UNAVAILABLE",
                    "message": "Database temporarily unavailable.",
                }
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
        sentry_sdk.capture_exception(exc)
        logger.exception("unhandled_server_error")
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "Internal server error.",
                }
            },
        )

    @app.get("/healthz")
    async def healthz(request: Request) -> JSONResponse:
        db_ok = await request.app.state.db.ping()
        nodes = request.app.state.state.node_registry.all_nodes()
        healthy_nodes = [n.region_code for n in nodes if n.is_healthy and n.is_active]
        payload = {
            "status": "ok" if db_ok else "degraded",
            "db": "ok" if db_ok else "down",
            "healthy_nodes": healthy_nodes,
            "total_nodes": len(nodes),
            "active_sessions": request.app.state.state.sessions.active_count,
        }
        return JSONResponse(status_code=200 if db_ok else 503, content=payload)

    @app.post("/v1/tests/init", response_model=TestInitResponse)
    async def tests_init(
        request: Request,
        response: Response,
        body: TestInitRequest | None = None,
        x_turnstile_token: str | None = Header(default=None, alias="X-Turnstile-Token"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> TestInitResponse:
        response.headers["Cache-Control"] = "no-store"
        raw_ip = _extract_client_ip(request)
        return await request.app.state.diagnostics.init_test(
            req=body or TestInitRequest(),
            raw_ip=raw_ip,
            turnstile_token=x_turnstile_token,
            idempotency_key=idempotency_key,
        )

    @app.post("/v1/tests/complete", response_model=TestCompleteResponse)
    async def tests_complete(
        request: Request,
        response: Response,
        body: TestCompleteRequest,
    ) -> TestCompleteResponse:
        response.headers["Cache-Control"] = "no-store"
        return await request.app.state.diagnostics.complete_test(body)

    @app.get("/v1/heatmap", response_model=HeatmapResponse)
    async def heatmap(
        request: Request,
        response: Response,
        carrier: str | None = Query(default=None),
        window: HeatmapWindow = Query(default="24h"),
        res: int = Query(default=6),
    ) -> HeatmapResponse:
        if res not in (6, 8):
            raise BadRequestError("Unsupported H3 resolution; expected 6 or 8.")
        response.headers["Cache-Control"] = "public, max-age=30, stale-while-revalidate=60"
        return await request.app.state.analytics.get_heatmap(
            carrier_slug=carrier,
            window=window,
            resolution=res,
        )

    @app.get("/v1/incidents/active", response_model=ActiveIncidentsResponse)
    async def active_incidents(request: Request, response: Response) -> ActiveIncidentsResponse:
        response.headers["Cache-Control"] = "public, max-age=15, stale-while-revalidate=30"
        return await request.app.state.analytics.get_active_incidents()

    return app


app = create_app()