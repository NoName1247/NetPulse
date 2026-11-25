from datetime import UTC, date, datetime
from typing import Any

import h3
import pytest
from fastapi.testclient import TestClient

from central.alerts import TelegramAlerter
from central.config import Settings
from central.db import Database
from central.diagnostics import TurnstileVerifier
from central.geo import GeoIPService
from central.main import create_app
from central.models import (
    AsnRecord,
    CarrierRecord,
    MeasurementRow,
    NetClass,
    NodeConfigRow,
    ReferenceData,
)
from central.state import current_myt_date

BUKIT_JALIL_LAT = 3.0554
BUKIT_JALIL_LON = 101.7005
BUKIT_JALIL_H3_RES6_HEX = h3.latlng_to_cell(BUKIT_JALIL_LAT, BUKIT_JALIL_LON, 6)
BUKIT_JALIL_H3_RES6_INT = h3.str_to_int(BUKIT_JALIL_H3_RES6_HEX)


def make_test_reference_data() -> ReferenceData:
    carriers = {
        1: CarrierRecord(id=1, slug="maxis", display_name="Maxis / Hotlink", carrier_type=1),
        4: CarrierRecord(id=4, slug="unifi", display_name="Unifi (Telekom Malaysia)", carrier_type=2),
    }
    return ReferenceData(
        carriers_by_id=carriers,
        carriers_by_slug={c.slug: c for c in carriers.values()},
        asn_map={
            9534: AsnRecord(
                asn=9534, carrier_id=1, net_class=NetClass.MOBILE_CARRIER, org_name="Maxis Broadband"
            ),
            4788: AsnRecord(
                asn=4788, carrier_id=4, net_class=NetClass.FIXED_BROADBAND, org_name="Telekom Malaysia"
            ),
            13335: AsnRecord(
                asn=13335, carrier_id=None, net_class=NetClass.VPN_DATACENTER, org_name="Cloudflare WARP"
            ),
        },
        nodes=[
            NodeConfigRow(
                id=1,
                region_code="MY-KUL",
                endpoint_url="https://my-node.netpulse.my",
                is_local=True,
                is_active=True,
            ),
            NodeConfigRow(
                id=2,
                region_code="EU-FRA",
                endpoint_url="https://eu-node.netpulse.my",
                is_local=False,
                is_active=True,
            ),
        ],
        districts_by_h3={BUKIT_JALIL_H3_RES6_INT: "Bukit Jalil"},
    )


class FakeDatabase(Database):
    def __init__(self) -> None:
        super().__init__("postgresql://unused")
        self.ref_data = make_test_reference_data()
        self.salt_day: date = current_myt_date()
        self.salt: bytes = b"test-daily-salt-32-bytes-0000001"
        self.inserted_rows: list[MeasurementRow] = []
        self.district_stats_row: dict[str, Any] = {
            "unique_sessions_1h": 24,
            "avg_stall_ratio_1h": 0.01,
            "p95_rtt_ms_1h": 42.0,
            "median_dl_kbps_1h": 15000.0,
            "samples_24h": 46,
        }
        self.heatmap_rows: list[dict[str, Any]] = [
            {
                "h3_cell": BUKIT_JALIL_H3_RES6_INT,
                "district_name": "Bukit Jalil",
                "unique_sessions": 64,
                "avg_stall_ratio": 0.01,
                "p95_rtt_ms": 45.2,
                "median_dl_kbps": 14200.0,
            }
        ]
        self.heatmap_queries = 0
        self.incident_rows: list[dict[str, Any]] = [
            {
                "id": 104,
                "carrier_slug": "unifi",
                "carrier_name": "Unifi (Telekom Malaysia)",
                "district_name": "Bukit Jalil",
                "h3_res6": BUKIT_JALIL_H3_RES6_INT,
                "severity": "MAJOR_OUTAGE",
                "verdict_code": "DISTRICT_CARRIER_OUTAGE",
                "unique_reports": 38,
                "avg_stall_bps": 2800,
                "p95_rtt_ms": 240.5,
                "started_at": datetime(2026, 11, 25, 15, 30, tzinfo=UTC),
                "last_seen_at": datetime(2026, 11, 25, 16, 2, tzinfo=UTC),
            }
        ]

    async def ping(self) -> bool:
        return True

    async def load_reference_data(self) -> ReferenceData:
        return self.ref_data

    async def get_or_create_daily_salt_myt(self) -> tuple[date, bytes]:
        return self.salt_day, self.salt

    async def delete_expired_salts_myt(self) -> int:
        return 1

    async def insert_measurements(self, rows: list[MeasurementRow]) -> None:
        self.inserted_rows.extend(rows)

    async def fetch_heatmap_rows(
        self, *, window: str, carrier_id: int | None, resolution: int, min_samples: int = 5
    ) -> list[dict[str, Any]]:
        self.heatmap_queries += 1
        return list(self.heatmap_rows)

    async def fetch_district_stats_row(
        self, *, h3_res6: int, carrier_id: int | None
    ) -> dict[str, Any] | None:
        return dict(self.district_stats_row)

    async def fetch_active_incidents_rows(self) -> list[dict[str, Any]]:
        return list(self.incident_rows)

    async def run_outage_detection_cycle(self) -> tuple[int, int]:
        return 0, 0

    async def delete_old_measurements_batch(
        self, *, retention_days: int = 180, limit: int = 50000
    ) -> int:
        return 0


class FakeGeoIPService(GeoIPService):
    def __init__(self) -> None:
        super().__init__("", "")

    def _lookup_asn(self, raw_ip: str) -> tuple[int, str]:
        if raw_ip.startswith("103.5."):
            return 9534, "Maxis Broadband Sdn Bhd"
        if raw_ip.startswith("60.50."):
            return 4788, "TM Technology Services Sdn. Bhd."
        if raw_ip == "1.1.1.1":
            return 13335, "Cloudflare, Inc."
        return 64512, "Generic Local ISP"

    def _lookup_city_coords(self, raw_ip: str) -> tuple[float, float, str | None]:
        return BUKIT_JALIL_LAT, BUKIT_JALIL_LON, "Kuala Lumpur"


class FakeAlerter(TelegramAlerter):
    def __init__(self) -> None:
        super().__init__("", "", cooldown_s=0)
        self.sent_alerts: list[tuple[str, str]] = []

    async def _send_message(self, dedup_key: str, text: str) -> bool:
        self.sent_alerts.append((dedup_key, text))
        return True


class FakeTurnstile(TurnstileVerifier):
    def __init__(self, should_pass: bool = True) -> None:
        super().__init__(enabled=True, secret_key="test", verify_url="https://example.invalid")
        self.should_pass = should_pass

    async def verify(self, token: str | None) -> bool:
        return self.should_pass


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        environment="test",
        node_secrets_json='{"1":"my_new_secret,my_old_secret","2":"eu_new_secret,eu_old_secret"}',
        turnstile_enabled=False,
        enable_scheduler=False,
        seed_districts_on_startup=False,
        rate_limit_fixed_max=15,
        rate_limit_mobile_max=120,
    )


@pytest.fixture
def fake_db() -> FakeDatabase:
    return FakeDatabase()


@pytest.fixture
def fake_geo() -> FakeGeoIPService:
    return FakeGeoIPService()


@pytest.fixture
def fake_alerter() -> FakeAlerter:
    return FakeAlerter()


@pytest.fixture
def client(
    test_settings: Settings,
    fake_db: FakeDatabase,
    fake_geo: FakeGeoIPService,
    fake_alerter: FakeAlerter,
):
    app = create_app(
        settings=test_settings,
        db=fake_db,
        geo=fake_geo,
        alerter=fake_alerter,
        turnstile=FakeTurnstile(should_pass=True),
    )
    with TestClient(app) as c:
        yield c