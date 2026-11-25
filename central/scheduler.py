import asyncio
import os
import time
from datetime import date, datetime

import httpx
import structlog

from central.alerts import TelegramAlerter
from central.config import Settings
from central.db import Database
from central.geo import GeoIPService
from central.models import NodeHealthState
from central.state import MYT_TZ, InMemoryState

logger = structlog.get_logger("central.scheduler")


class BackgroundScheduler:
    """
    In-process asyncio-планировщик внутри единственного воркера Uvicorn (0 Split-Brain):
    1) Pull Healthcheck нод каждые 5с + контроль Clock Skew (>5s -> алерт).
    2) SQL-детектор массовых аварий каждые 2 мин (UPSERT + автозакрытие resolved_at).
    3) Суточная ротация/очистка daily_salts в 00:05 MYT и ретеншн 180d в 03:00 MYT.
    4) Hot-reload GeoLite2 .mmdb при обновлении файлов на диске.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        state: InMemoryState,
        geo: GeoIPService,
        alerter: TelegramAlerter,
        http_client: httpx.AsyncClient,
    ) -> None:
        self._settings = settings
        self._db = db
        self._state = state
        self._geo = geo
        self._alerter = alerter
        self._http_client = http_client
        self._stop_event = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

        self._last_salt_cleanup_day: date | None = None
        self._last_retention_day: date | None = None
        self._geoip_mtimes: dict[str, float] = self._read_geoip_mtimes()
        self._healthcheck_overrides: dict[int, str] = settings.parsed_healthcheck_urls()

    def start(self) -> None:
        if self._tasks:
            return
        self._stop_event.clear()
        self._tasks = [
            asyncio.create_task(self._healthcheck_loop(), name="probe-healthcheck"),
            asyncio.create_task(self._outage_detector_loop(), name="outage-detector"),
            asyncio.create_task(self._daily_maintenance_loop(), name="daily-maintenance-myt"),
            asyncio.create_task(self._geoip_reload_loop(), name="geoip-reload"),
        ]
        logger.info("background_scheduler_started", task_count=len(self._tasks))

    async def stop(self) -> None:
        self._stop_event.set()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
        logger.info("background_scheduler_stopped")

    async def _sleep_or_stop(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
            return True
        except TimeoutError:
            return False

    async def poll_single_node(self, node: NodeHealthState) -> None:
        base_url = self._healthcheck_overrides.get(node.node_id, node.endpoint_url)
        url = f"{base_url}/healthz"
        try:
            resp = await self._http_client.get(url, timeout=self._settings.healthcheck_timeout_s)
            if resp.status_code != 200:
                raise RuntimeError(f"Non-200 status: {resp.status_code}")

            body = resp.json()
            if body.get("status") != "ok":
                raise RuntimeError("Node reported non-ok status")

            mode = body.get("mode")
            if mode not in ("FULL_PROBE", "PULSE_ONLY"):
                raise RuntimeError(f"Unexpected probe mode: {mode}")

            active_sessions = int(body.get("active_sessions", 0))
            server_time = float(body.get("server_time", time.time()))
            drift_s = server_time - time.time()

            self._state.node_registry.record_success(
                node.node_id,
                mode=mode,
                active_sessions=active_sessions,
                clock_drift_s=drift_s,
            )

            if abs(drift_s) > self._settings.clock_skew_alert_s:
                logger.warning(
                    "node_clock_skew_detected",
                    node_id=node.node_id,
                    region=node.region_code,
                    drift_s=round(drift_s, 2),
                )
                await self._alerter.alert_clock_skew(node.node_id, node.region_code, drift_s)

        except Exception as exc:
            transitioned_down, failures = self._state.node_registry.record_failure(node.node_id)
            logger.warning(
                "node_healthcheck_failed",
                node_id=node.node_id,
                region=node.region_code,
                consecutive_failures=failures,
                error=str(exc),
            )
            if transitioned_down:
                await self._alerter.alert_node_down(node.node_id, node.region_code, failures)

    async def poll_all_nodes_once(self) -> None:
        nodes = [n for n in self._state.node_registry.all_nodes() if n.is_active]
        if not nodes:
            return
        await asyncio.gather(*(self.poll_single_node(node) for node in nodes))

    async def _healthcheck_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.poll_all_nodes_once()
            except Exception:
                logger.exception("healthcheck_loop_error")
            if await self._sleep_or_stop(self._settings.healthcheck_interval_s):
                break

    async def _outage_detector_loop(self) -> None:
        while not self._stop_event.is_set():
            if await self._sleep_or_stop(self._settings.outage_detector_interval_s):
                break
            try:
                upserted, resolved = await self._db.run_outage_detection_cycle()
                cleaned_sessions = self._state.sessions.cleanup_expired()
                self._state.failed_nodes.cleanup_expired()
                if upserted or resolved:
                    logger.info(
                        "outage_detection_cycle_done",
                        upserted=upserted,
                        resolved=resolved,
                        cleaned_sessions=cleaned_sessions,
                    )
            except Exception:
                logger.exception("outage_detector_cycle_failed")

    async def _daily_maintenance_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                now_myt = datetime.now(MYT_TZ)
                today_myt = now_myt.date()

                # 00:05 MYT: удаление вчерашних солей и гарантированное создание сегодняшней
                if (
                    now_myt.hour == 0
                    and now_myt.minute >= 5
                    and self._last_salt_cleanup_day != today_myt
                ):
                    deleted_salts = await self._db.delete_expired_salts_myt()
                    self._state.salts.invalidate()
                    salt_day, salt = await self._db.get_or_create_daily_salt_myt()
                    self._state.salts.set(salt_day, salt)
                    self._last_salt_cleanup_day = today_myt
                    logger.info(
                        "daily_salt_rotated_myt",
                        salt_day=str(salt_day),
                        deleted_old_salts=deleted_salts,
                    )

                # 03:00 MYT: ночная очистка сырых замеров старше 180 дней батчами по 50k
                if now_myt.hour == 3 and self._last_retention_day != today_myt:
                    total_deleted = 0
                    while not self._stop_event.is_set():
                        deleted = await self._db.delete_old_measurements_batch(
                            retention_days=self._settings.retention_days,
                            limit=self._settings.retention_batch_size,
                        )
                        total_deleted += deleted
                        if deleted < self._settings.retention_batch_size:
                            break
                        await asyncio.sleep(0.1)
                    self._last_retention_day = today_myt
                    logger.info(
                        "retention_cleanup_completed",
                        retention_days=self._settings.retention_days,
                        deleted_rows=total_deleted,
                    )
            except Exception:
                logger.exception("daily_maintenance_loop_error")

            if await self._sleep_or_stop(60.0):
                break

    def _read_geoip_mtimes(self) -> dict[str, float]:
        result: dict[str, float] = {}
        for path in (self._settings.geolite2_asn_path, self._settings.geolite2_city_path):
            try:
                result[path] = os.path.getmtime(path)
            except OSError:
                result[path] = 0.0
        return result

    async def _geoip_reload_loop(self) -> None:
        while not self._stop_event.is_set():
            if await self._sleep_or_stop(self._settings.geoip_reload_interval_s):
                break
            try:
                current_mtimes = self._read_geoip_mtimes()
                if current_mtimes != self._geoip_mtimes:
                    self._geo.load_databases()
                    self._geoip_mtimes = current_mtimes
                    logger.info("geoip_databases_reloaded")
            except Exception:
                logger.exception("geoip_reload_failed")