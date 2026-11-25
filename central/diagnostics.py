import os
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import structlog

import shared_proto as proto
from central.alerts import TelegramAlerter
from central.analytics import AnalyticsService, evaluate_verdict
from central.config import Settings
from central.db import Database
from central.geo import GeoIPService, subnet_key
from central.models import (
    BadRequestError,
    DetectedNetwork,
    DistrictComparison,
    InvalidSessionError,
    MeasurementRow,
    NetClass,
    ProbeModeDB,
    ProbeModeStr,
    ProbePlanItem,
    RateLimitError,
    ReceiptIn,
    ServiceUnavailableError,
    TamperedReceiptError,
    TestCompleteRequest,
    TestCompleteResponse,
    TestInitRequest,
    TestInitResponse,
    TestSession,
    TurnstileError,
    VerdictMetrics,
)
from central.state import InMemoryState

logger = structlog.get_logger("central.diagnostics")


def generate_uuidv7() -> UUID:
    """RFC 9562 UUIDv7 (монотонно возрастающий 48-битный Unix ms timestamp + 74 бит случайности)."""
    unix_ts_ms = time.time_ns() // 1_000_000
    rand_bytes = os.urandom(10)
    rand_a = int.from_bytes(rand_bytes[:2], "big") & 0x0FFF
    rand_b = int.from_bytes(rand_bytes[2:], "big") & 0x3FFFFFFFFFFFFFFF

    uuid_int = (
        ((unix_ts_ms & 0xFFFFFFFFFFFF) << 80)
        | (0x7 << 76)
        | (rand_a << 64)
        | (0b10 << 62)
        | rand_b
    )
    return UUID(int=uuid_int)


class TurnstileVerifier:
    """Верификация токена Cloudflare Turnstile на сервере."""

    def __init__(
        self,
        *,
        enabled: bool,
        secret_key: str,
        verify_url: str,
        timeout_s: float = 3.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._enabled = enabled
        self._secret_key = secret_key
        self._verify_url = verify_url
        self._timeout_s = timeout_s
        self._http_client = http_client

    async def verify(self, token: str | None) -> bool:
        if not self._enabled:
            return True
        if not token or not token.strip():
            return False
        if not self._secret_key:
            logger.error("turnstile_secret_missing")
            return False

        payload = {"secret": self._secret_key, "response": token.strip()}
        try:
            if self._http_client is not None:
                resp = await self._http_client.post(
                    self._verify_url, data=payload, timeout=self._timeout_s
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                    resp = await client.post(self._verify_url, data=payload)
            if resp.status_code != 200:
                logger.warning("turnstile_http_non_200", status_code=resp.status_code)
                return False
            body = resp.json()
            return bool(body.get("success") is True)
        except Exception:
            logger.exception("turnstile_verify_error")
            return False


class DiagnosticsService:
    """Bounded Context диагностики: init -> подписанный план -> complete -> вердикт."""

    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        state: InMemoryState,
        geo: GeoIPService,
        analytics: AnalyticsService,
        alerter: TelegramAlerter,
        turnstile: TurnstileVerifier,
    ) -> None:
        self._settings = settings
        self._db = db
        self._state = state
        self._geo = geo
        self._analytics = analytics
        self._alerter = alerter
        self._turnstile = turnstile
        self._node_secrets: dict[int, list[str]] = settings.parsed_node_secrets()

    async def _get_daily_salt(self) -> bytes:
        cached = self._state.salts.get()
        if cached is not None:
            return cached
        salt_day, salt = await self._db.get_or_create_daily_salt_myt()
        self._state.salts.set(salt_day, salt)
        return salt

    async def init_test(
        self,
        *,
        req: TestInitRequest,
        raw_ip: str,
        turnstile_token: str | None,
        idempotency_key: str | None,
    ) -> TestInitResponse:
        if idempotency_key:
            cached_resp = self._state.sessions.get_idempotent(idempotency_key)
            if cached_resp is not None:
                return cached_resp

        daily_salt = await self._get_daily_salt()
        ctx = self._geo.classify_and_quantize(
            raw_ip=raw_ip,
            client_geo=req.client_geo,
            daily_salt=daily_salt,
            ref_data=self._state.ref_data,
        )

        # CGNAT-safe Token Bucket проверяется ДО вызова Turnstile (защита от outbound flood)
        bucket_key = subnet_key(raw_ip)
        allowed, retry_after_s = self._state.rate_limiter.check_and_consume(
            bucket_key, ctx.net_class
        )
        if not allowed:
            raise RateLimitError(retry_after_s)

        if not await self._turnstile.verify(turnstile_token):
            raise TurnstileError("Cloudflare Turnstile verification failed.")

        local_node, intl_nodes = self._state.node_registry.get_healthy_plan_nodes()
        if local_node is None and not intl_nodes:
            raise ServiceUnavailableError("No healthy probe nodes are currently available.")

        test_id = str(generate_uuidv7())
        probe_plan: list[ProbePlanItem] = []
        expected_nodes: dict[int, ProbeModeStr] = {}
        local_node_ids: set[int] = set()

        if local_node is not None:
            secrets = self._node_secrets.get(local_node.node_id)
            if secrets:
                mode: ProbeModeStr = (
                    "PULSE_ONLY" if local_node.mode == "PULSE_ONLY" else "FULL_PROBE"
                )
                token = proto.sign_probe_token(
                    secrets,
                    test_id,
                    local_node.node_id,
                    mode,
                    ttl_s=self._settings.probe_token_ttl_s,
                )
                probe_plan.append(
                    ProbePlanItem(
                        node_id=local_node.node_id,
                        region=local_node.region_code,
                        wss_url=local_node.wss_url,
                        probe_mode=mode,
                        probe_token=token,
                    )
                )
                expected_nodes[local_node.node_id] = mode
                local_node_ids.add(local_node.node_id)

        for intl_node in intl_nodes:
            secrets = self._node_secrets.get(intl_node.node_id)
            if not secrets:
                continue
            # К зарубежной ноде всегда PULSE_ONLY (экономия трафика и стабильность замера)
            intl_mode: ProbeModeStr = "PULSE_ONLY"
            token = proto.sign_probe_token(
                secrets,
                test_id,
                intl_node.node_id,
                intl_mode,
                ttl_s=self._settings.probe_token_ttl_s,
            )
            probe_plan.append(
                ProbePlanItem(
                    node_id=intl_node.node_id,
                    region=intl_node.region_code,
                    wss_url=intl_node.wss_url,
                    probe_mode=intl_mode,
                    probe_token=token,
                )
            )
            expected_nodes[intl_node.node_id] = intl_mode

        if not probe_plan:
            raise ServiceUnavailableError("Probe fleet secrets are not configured.")

        local_probe_unavailable = len(local_node_ids) == 0
        session = TestSession(
            test_id=test_id,
            expires_mono=time.monotonic() + self._settings.session_ttl_s,
            carrier_id=ctx.carrier_id,
            carrier_slug=ctx.carrier_slug,
            carrier_name=ctx.carrier_name,
            asn=ctx.asn,
            net_class=ctx.net_class,
            h3_res8_int=ctx.h3_res8_int,
            h3_res6_int=ctx.h3_res6_int,
            h3_res8_hex=ctx.h3_res8_hex,
            h3_res6_hex=ctx.h3_res6_hex,
            district_name=ctx.district_name,
            geo_source=ctx.geo_source,
            session_hash=ctx.session_hash,
            expected_nodes=expected_nodes,
            local_node_ids=local_node_ids,
            local_probe_unavailable=local_probe_unavailable,
        )
        self._state.sessions.put(session)

        expires_at = datetime.now(UTC) + timedelta(seconds=self._settings.session_ttl_s)
        response = TestInitResponse(
            test_id=test_id,
            expires_at=expires_at,
            detected_network=DetectedNetwork(
                asn=ctx.asn,
                carrier_slug=ctx.carrier_slug,
                carrier_name=ctx.carrier_name,
                net_class=ctx.net_class.to_api_str(),
                exclude_from_heatmap=ctx.net_class
                not in (NetClass.MOBILE_CARRIER, NetClass.FIXED_BROADBAND),
                h3_res8=ctx.h3_res8_hex,
                h3_res6=ctx.h3_res6_hex,
                district_name=ctx.district_name,
                geo_source=ctx.geo_source.to_api_str(),
            ),
            probe_plan=probe_plan,
        )
        if idempotency_key:
            self._state.sessions.put_idempotent(idempotency_key, response)

        logger.info(
            "test_initialized",
            test_id=test_id,
            carrier_slug=ctx.carrier_slug,
            net_class=ctx.net_class.to_api_str(),
            h3_res6=ctx.h3_res6_hex,
            nodes=list(expected_nodes.keys()),
            local_probe_unavailable=local_probe_unavailable,
        )
        return response

    async def complete_test(self, req: TestCompleteRequest) -> TestCompleteResponse:
        # Атомарное извлечение сессии -> мгновенная защита от Replay-атаки
        session = self._state.sessions.pop(req.test_id)
        if session is None:
            raise InvalidSessionError("Invalid or expired test session.")

        if not req.receipts and not req.failed_nodes:
            raise BadRequestError("At least one receipt or failed node entry is required.")

        seen_node_ids: set[int] = set()
        verified_receipts: list[ReceiptIn] = []

        for receipt in req.receipts:
            if receipt.node_id not in session.expected_nodes:
                raise TamperedReceiptError("Receipt references an unexpected node_id.")
            if receipt.node_id in seen_node_ids:
                raise TamperedReceiptError("Duplicate node_id in receipts.")

            planned_mode = session.expected_nodes[receipt.node_id]
            # Нода может понизить FULL_PROBE -> PULSE_ONLY под нагрузкой, но не наоборот
            if planned_mode == "PULSE_ONLY" and receipt.probe_mode != "PULSE_ONLY":
                raise TamperedReceiptError("Probe mode elevation detected.")

            if receipt.probe_mode == "PULSE_ONLY" and (
                receipt.metrics.dl_kbps is not None or receipt.metrics.ul_kbps is not None
            ):
                raise TamperedReceiptError("Throughput metrics must be null for PULSE_ONLY.")

            secrets = self._node_secrets.get(receipt.node_id)
            if not secrets:
                raise TamperedReceiptError("Unknown probe node credentials.")

            is_valid = proto.verify_receipt(
                secrets,
                receipt.signature_hmac,
                test_id=req.test_id,
                node_id=receipt.node_id,
                actual_mode=receipt.probe_mode,
                measured_at=receipt.measured_at,
                rtt_p50_ms=receipt.metrics.rtt_p50_ms,
                rtt_p95_ms=receipt.metrics.rtt_p95_ms,
                jitter_ms=receipt.metrics.jitter_ms,
                stall_ratio=receipt.metrics.stall_ratio,
                dl_kbps=receipt.metrics.dl_kbps,
                ul_kbps=receipt.metrics.ul_kbps,
            )
            if not is_valid:
                logger.warning(
                    "tampered_receipt_rejected",
                    test_id=req.test_id,
                    node_id=receipt.node_id,
                )
                raise TamperedReceiptError("Invalid canonical receipt HMAC signature.")

            seen_node_ids.add(receipt.node_id)
            verified_receipts.append(receipt)

        for failed in req.failed_nodes:
            if failed.node_id not in session.expected_nodes:
                raise BadRequestError("failed_nodes references an unexpected node_id.")
            if failed.node_id in seen_node_ids:
                raise BadRequestError("Duplicate node_id across receipts and failed_nodes.")
            seen_node_ids.add(failed.node_id)

        # Прямая синхронная вставка в PostgreSQL (~2ms)
        test_uuid = UUID(req.test_id)
        db_rows = [
            MeasurementRow(
                test_id=test_uuid,
                node_id=r.node_id,
                carrier_id=session.carrier_id,
                asn=session.asn,
                net_class=int(session.net_class),
                h3_res8=session.h3_res8_int,
                h3_res6=session.h3_res6_int,
                geo_source=int(session.geo_source),
                probe_mode=int(ProbeModeDB.from_str(r.probe_mode)),
                rtt_p50_ms=r.metrics.rtt_p50_ms,
                rtt_p95_ms=r.metrics.rtt_p95_ms,
                jitter_ms=r.metrics.jitter_ms,
                stall_ratio=r.metrics.stall_ratio,
                dl_kbps=None if r.probe_mode == "PULSE_ONLY" else r.metrics.dl_kbps,
                ul_kbps=None if r.probe_mode == "PULSE_ONLY" else r.metrics.ul_kbps,
                session_hash=session.session_hash,
            )
            for r in verified_receipts
        ]
        if db_rows:
            await self._db.insert_measurements(db_rows)
            if session.net_class in (NetClass.MOBILE_CARRIER, NetClass.FIXED_BROADBAND):
                self._state.heatmap_cache.clear()

        # Детектор обрыва магистрали/пиринга: >= 30 уник. пользователей за 5 мин к живой ноде
        if (
            session.carrier_id is not None
            and session.net_class in (NetClass.MOBILE_CARRIER, NetClass.FIXED_BROADBAND)
        ):
            for failed in req.failed_nodes:
                if self._state.node_registry.is_node_healthy(failed.node_id):
                    unique_reports = self._state.failed_nodes.record_failure(
                        session.carrier_id, failed.node_id, session.session_hash
                    )
                    if unique_reports >= self._settings.peering_alert_threshold:
                        node_state = self._state.node_registry.get_node(failed.node_id)
                        region = node_state.region_code if node_state else f"node-{failed.node_id}"
                        await self._alerter.alert_peering_outage(
                            carrier_name=session.carrier_name,
                            node_id=failed.node_id,
                            region_code=region,
                            unique_reports=unique_reports,
                        )

        district_stats = await self._analytics.get_district_stats(
            h3_res6=session.h3_res6_int,
            carrier_id=session.carrier_id,
        )

        my_receipt = next(
            (r for r in verified_receipts if r.node_id in session.local_node_ids),
            None,
        )
        eu_receipt = next(
            (r for r in verified_receipts if r.node_id not in session.local_node_ids),
            None,
        )

        verdict = evaluate_verdict(
            my_receipt=my_receipt,
            eu_receipt=eu_receipt,
            failed_nodes=req.failed_nodes,
            local_node_ids=session.local_node_ids,
            district_stats=district_stats,
            net_class=session.net_class,
            carrier_name=session.carrier_name,
            district_name=session.district_name,
            local_probe_unavailable=session.local_probe_unavailable,
        )

        metrics_out = VerdictMetrics(
            local_rtt_p50_ms=round(my_receipt.metrics.rtt_p50_ms, 1) if my_receipt else None,
            intl_rtt_p50_ms=round(eu_receipt.metrics.rtt_p50_ms, 1) if eu_receipt else None,
            dl_mbps=(
                round(my_receipt.metrics.dl_kbps / 1000.0, 2)
                if (my_receipt and my_receipt.metrics.dl_kbps is not None)
                else None
            ),
            ul_mbps=(
                round(my_receipt.metrics.ul_kbps / 1000.0, 2)
                if (my_receipt and my_receipt.metrics.ul_kbps is not None)
                else None
            ),
            stall_ratio=(
                my_receipt.metrics.stall_ratio
                if my_receipt
                else (eu_receipt.metrics.stall_ratio if eu_receipt else 1.0)
            ),
        )

        logger.info(
            "test_completed",
            test_id=req.test_id,
            carrier_slug=session.carrier_slug,
            h3_res6=session.h3_res6_hex,
            verdict_code=verdict.verdict_code.value,
            receipt_count=len(verified_receipts),
            failed_count=len(req.failed_nodes),
        )

        return TestCompleteResponse(
            test_id=req.test_id,
            carrier=session.carrier_name,
            district=session.district_name,
            verdict_code=verdict.verdict_code,
            verdict_text_ru=verdict.verdict_text_ru,
            verdict_text_en=verdict.verdict_text_en,
            verdict_text_bm=verdict.verdict_text_bm,
            local_probe_unavailable=session.local_probe_unavailable,
            metrics=metrics_out,
            district_comparison=DistrictComparison(
                district_health_score=district_stats.health_score,
                status=district_stats.status,
                samples_24h=district_stats.samples_24h,
            ),
        )