import time
from datetime import timedelta

import httpx
import pytest

from central.config import Settings
from central.geo import compute_session_hash, subnet_key
from central.logging_setup import strip_pii_fields
from central.models import NodeConfigRow
from central.scheduler import BackgroundScheduler
from central.state import (
    FailedNodeTracker,
    InMemoryState,
    SaltCache,
    current_myt_date,
)
from tests.conftest import FakeAlerter, FakeDatabase, FakeGeoIPService


def test_strip_pii_fields_removes_raw_ip_and_gps_recursively():
    event = {
        "event": "test_initialized",
        "raw_ip": "103.5.180.10",
        "cf-connecting-ip": "103.5.180.10",
        "lat": 3.0554,
        "lon": 101.7005,
        "nested": {"client_ip": "1.2.3.4", "h3_res6": "866506667ffffff"},
        "carrier_slug": "maxis",
    }
    cleaned = strip_pii_fields(None, "info", event)
    assert "raw_ip" not in cleaned
    assert "cf-connecting-ip" not in cleaned
    assert "lat" not in cleaned
    assert "lon" not in cleaned
    assert cleaned["nested"]["client_ip"] == "[REDACTED]"
    assert cleaned["nested"]["h3_res6"] == "866506667ffffff"
    assert cleaned["carrier_slug"] == "maxis"


def test_session_hash_and_subnet_key():
    h1 = compute_session_hash("103.5.180.10", b"salt-day-1")
    h2 = compute_session_hash("103.5.180.10", b"salt-day-2")
    assert len(h1) == 16
    assert h1 != h2

    assert subnet_key("60.50.10.221") == "v4:60.50.10.0/24"
    assert subnet_key("2001:db8:abcd:12::99") == "v6:2001:db8:abcd:12::/64"


def test_salt_cache_invalidates_on_myt_day_rollover():
    cache = SaltCache(ttl_s=300)
    yesterday_myt = current_myt_date() - timedelta(days=1)
    cache.set(yesterday_myt, b"old-salt")
    assert cache.get() is None

    today_myt = current_myt_date()
    cache.set(today_myt, b"today-salt")
    assert cache.get() == b"today-salt"


def test_failed_node_tracker_counts_unique_sessions_in_window():
    tracker = FailedNodeTracker(window_s=300)
    # Один и тот же session_hash не накручивает счётчик
    assert tracker.record_failure(1, 2, b"user-a") == 1
    assert tracker.record_failure(1, 2, b"user-a") == 1
    assert tracker.record_failure(1, 2, b"user-b") == 2


@pytest.mark.asyncio
async def test_scheduler_detects_clock_skew_and_node_down():
    settings = Settings(
        environment="test",
        node_secrets_json='{"1":"s1","2":"s2"}',
        clock_skew_alert_s=5.0,
        healthcheck_fail_threshold=3,
    )
    state = InMemoryState(healthcheck_fail_threshold=3)
    state.node_registry.load_nodes(
        [
            NodeConfigRow(1, "MY-KUL", "https://my-node.test", True, True),
            NodeConfigRow(2, "EU-FRA", "https://eu-node.test", False, True),
        ]
    )
    alerter = FakeAlerter()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "my-node.test":
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "node_id": 1,
                    "mode": "FULL_PROBE",
                    "active_sessions": 4,
                    "server_time": int(time.time()) + 12,  # +12s Clock Skew (> 5s!)
                },
            )
        return httpx.Response(503)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        scheduler = BackgroundScheduler(
            settings=settings,
            db=FakeDatabase(),
            state=state,
            geo=FakeGeoIPService(),
            alerter=alerter,
            http_client=http_client,
        )
        await scheduler.poll_all_nodes_once()
        await scheduler.poll_all_nodes_once()
        await scheduler.poll_all_nodes_once()

    assert state.node_registry.is_node_healthy(1) is True
    assert state.node_registry.is_node_healthy(2) is False
    alert_keys = [k for k, _ in alerter.sent_alerts]
    assert "clock_skew:1" in alert_keys
    assert "node_down:2" in alert_keys