import time

import httpx
import structlog

logger = structlog.get_logger("central.alerts")


class TelegramAlerter:
    """Асинхронная отправка алертов в Telegram с антиспам-cooldown в RAM."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        cooldown_s: int = 300,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._bot_token = bot_token.strip()
        self._chat_id = chat_id.strip()
        self._cooldown_s = cooldown_s
        self._http_client = http_client
        self._last_sent_mono: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return bool(self._bot_token and self._chat_id)

    def _can_send(self, dedup_key: str) -> bool:
        now = time.monotonic()
        last = self._last_sent_mono.get(dedup_key)
        if last is not None and (now - last) < self._cooldown_s:
            return False
        self._last_sent_mono[dedup_key] = now
        return True

    async def _send_message(self, dedup_key: str, text: str) -> bool:
        if not self._can_send(dedup_key):
            return False
        if not self.enabled:
            logger.warning("telegram_alert_not_configured", dedup_key=dedup_key, alert_text=text)
            return False

        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        payload = {"chat_id": self._chat_id, "text": text, "disable_web_page_preview": True}
        try:
            if self._http_client is not None:
                resp = await self._http_client.post(url, json=payload, timeout=5.0)
            else:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.post(url, json=payload)
            if resp.status_code >= 400:
                logger.error("telegram_alert_http_error", status_code=resp.status_code)
                return False
            return True
        except Exception:
            logger.exception("telegram_alert_send_failed", dedup_key=dedup_key)
            return False

    async def alert_clock_skew(self, node_id: int, region_code: str, drift_s: float) -> bool:
        msg = (
            f"[NetPulse Clock Skew] Node {region_code} (id={node_id}) clock drift "
            f"is {drift_s:+.1f}s (> 5.0s threshold). Check NTP/chrony."
        )
        return await self._send_message(f"clock_skew:{node_id}", msg)

    async def alert_node_down(
        self, node_id: int, region_code: str, consecutive_failures: int
    ) -> bool:
        msg = (
            f"[NetPulse Node DOWN] Probe node {region_code} (id={node_id}) "
            f"failed {consecutive_failures} consecutive /healthz checks and was excluded from plan."
        )
        return await self._send_message(f"node_down:{node_id}", msg)

    async def alert_peering_outage(
        self, carrier_name: str, node_id: int, region_code: str, unique_reports: int
    ) -> bool:
        msg = (
            f"[NetPulse Peering Alert] {unique_reports} unique users on {carrier_name} "
            f"reported connection failures to healthy node {region_code} (id={node_id}) in 5m. "
            "Possible carrier peering or international gateway outage."
        )
        return await self._send_message(f"peering:{carrier_name}:{node_id}", msg)