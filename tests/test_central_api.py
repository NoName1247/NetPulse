import time

from fastapi.testclient import TestClient

import shared_proto as proto
from central.config import Settings
from central.main import create_app
from tests.conftest import (
    BUKIT_JALIL_H3_RES6_HEX,
    BUKIT_JALIL_LAT,
    BUKIT_JALIL_LON,
    FakeAlerter,
    FakeDatabase,
    FakeGeoIPService,
    FakeTurnstile,
)


def _signed_receipt(
    *,
    secret: str,
    test_id: str,
    node_id: int,
    probe_mode: str,
    rtt_p50_ms: float = 28.4,
    rtt_p95_ms: float = 42.1,
    jitter_ms: float = 4.2,
    stall_ratio: float = 0.0,
    dl_kbps: int | None = 12500,
    ul_kbps: int | None = 3200,
) -> dict:
    measured_at = int(time.time())
    sig = proto.sign_receipt(
        [secret],
        test_id=test_id,
        node_id=node_id,
        actual_mode=probe_mode,
        measured_at=measured_at,
        rtt_p50_ms=rtt_p50_ms,
        rtt_p95_ms=rtt_p95_ms,
        jitter_ms=jitter_ms,
        stall_ratio=stall_ratio,
        dl_kbps=dl_kbps,
        ul_kbps=ul_kbps,
    )
    return {
        "node_id": node_id,
        "probe_mode": probe_mode,
        "measured_at": measured_at,
        "metrics": {
            "rtt_p50_ms": rtt_p50_ms,
            "rtt_p95_ms": rtt_p95_ms,
            "jitter_ms": jitter_ms,
            "stall_ratio": stall_ratio,
            "dl_kbps": dl_kbps,
            "ul_kbps": ul_kbps,
        },
        "signature_hmac": sig,
    }


def test_healthz(client: TestClient):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["healthy_nodes"] == ["MY-KUL", "EU-FRA"]


def test_full_init_idempotency_complete_and_replay_protection(
    client: TestClient, fake_db: FakeDatabase
):
    headers = {
        "CF-Connecting-IP": "103.5.180.10",
        "Idempotency-Key": "idem-key-001",
    }
    payload = {
        "client_geo": {"lat": BUKIT_JALIL_LAT, "lon": BUKIT_JALIL_LON},
        "connection_hint": {"effective_type": "4g"},
    }

    init1 = client.post("/v1/tests/init", json=payload, headers=headers)
    assert init1.status_code == 200
    assert init1.headers["cache-control"] == "no-store"
    data = init1.json()
    assert data["detected_network"]["carrier_slug"] == "maxis"
    assert data["detected_network"]["net_class"] == "MOBILE_CARRIER"
    assert data["detected_network"]["h3_res6"] == BUKIT_JALIL_H3_RES6_HEX
    assert data["detected_network"]["geo_source"] == "BROWSER_GPS"
    assert len(data["probe_plan"]) == 2
    assert data["probe_plan"][0]["probe_mode"] == "FULL_PROBE"
    assert data["probe_plan"][1]["probe_mode"] == "PULSE_ONLY"

    # Повтор с тем же Idempotency-Key возвращает ту же сессию
    init2 = client.post("/v1/tests/init", json=payload, headers=headers)
    assert init2.status_code == 200
    assert init2.json()["test_id"] == data["test_id"]

    # Подписываем чек MY старым ключом (проверка ротации "new,old"), EU отправляем в failed_nodes
    my_receipt = _signed_receipt(
        secret="my_old_secret",
        test_id=data["test_id"],
        node_id=1,
        probe_mode="FULL_PROBE",
    )
    complete = client.post(
        "/v1/tests/complete",
        json={
            "test_id": data["test_id"],
            "receipts": [my_receipt],
            "failed_nodes": [{"node_id": 2, "error": "TIMEOUT"}],
        },
    )
    assert complete.status_code == 200
    c_body = complete.json()
    assert c_body["verdict_code"] == "INTL_ROUTE_DEGRADED"
    assert c_body["metrics"]["local_rtt_p50_ms"] == 28.4
    assert c_body["metrics"]["intl_rtt_p50_ms"] is None
    assert c_body["metrics"]["dl_mbps"] == 12.5
    assert c_body["district_comparison"]["samples_24h"] == 46
    assert len(fake_db.inserted_rows) == 1
    assert fake_db.inserted_rows[0].dl_kbps == 12500

    # Replay-атака: повторная отправка того же test_id -> 403
    replay = client.post(
        "/v1/tests/complete",
        json={"test_id": data["test_id"], "receipts": [my_receipt], "failed_nodes": []},
    )
    assert replay.status_code == 403
    assert replay.json()["error"]["code"] == "INVALID_OR_EXPIRED_SESSION"


def test_complete_rejects_tampered_signature_and_mode_elevation(client: TestClient):
    init_resp = client.post("/v1/tests/init", json={}, headers={"CF-Connecting-IP": "103.5.180.11"})
    test_id = init_resp.json()["test_id"]

    tampered = _signed_receipt(
        secret="my_new_secret", test_id=test_id, node_id=1, probe_mode="FULL_PROBE"
    )
    tampered["metrics"]["dl_kbps"] = 50  # Подделка метрики после подписи

    resp = client.post(
        "/v1/tests/complete",
        json={"test_id": test_id, "receipts": [tampered], "failed_nodes": []},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "TAMPERED_RECEIPT"

    # Попытка повысить режим EU-ноды с PULSE_ONLY до FULL_PROBE
    init_resp2 = client.post("/v1/tests/init", json={}, headers={"CF-Connecting-IP": "103.5.180.12"})
    test_id2 = init_resp2.json()["test_id"]
    elevated_eu = _signed_receipt(
        secret="eu_new_secret", test_id=test_id2, node_id=2, probe_mode="FULL_PROBE"
    )
    resp2 = client.post(
        "/v1/tests/complete",
        json={"test_id": test_id2, "receipts": [elevated_eu], "failed_nodes": []},
    )
    assert resp2.status_code == 403
    assert resp2.json()["error"]["code"] == "TAMPERED_RECEIPT"


def test_vpn_user_gets_vpn_active_verdict(client: TestClient):
    init_resp = client.post("/v1/tests/init", json={}, headers={"CF-Connecting-IP": "1.1.1.1"})
    assert init_resp.status_code == 200
    init_data = init_resp.json()
    assert init_data["detected_network"]["net_class"] == "VPN_DATACENTER"
    assert init_data["detected_network"]["exclude_from_heatmap"] is True

    my_receipt = _signed_receipt(
        secret="my_new_secret", test_id=init_data["test_id"], node_id=1, probe_mode="FULL_PROBE"
    )
    complete = client.post(
        "/v1/tests/complete",
        json={"test_id": init_data["test_id"], "receipts": [my_receipt], "failed_nodes": []},
    )
    assert complete.status_code == 200
    assert complete.json()["verdict_code"] == "VPN_ACTIVE"


def test_rate_limit_fixed_vs_mobile_cgnat(client: TestClient):
    # Fixed Broadband (/24) ограничен 15 запросами
    for i in range(15):
        r = client.post("/v1/tests/init", json={}, headers={"CF-Connecting-IP": f"60.50.10.{i + 1}"})
        assert r.status_code == 200

    blocked = client.post("/v1/tests/init", json={}, headers={"CF-Connecting-IP": "60.50.10.99"})
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) >= 1

    # Mobile CGNAT (/24) спокойно проходит > 15 запросов (лимит 120)
    for i in range(20):
        r = client.post(
            "/v1/tests/init", json={}, headers={"CF-Connecting-IP": f"103.5.99.{i + 1}"}
        )
        assert r.status_code == 200


def test_heatmap_caching_headers_and_incidents(client: TestClient, fake_db: FakeDatabase):
    r1 = client.get("/v1/heatmap?carrier=maxis&window=24h&res=6")
    assert r1.status_code == 200
    assert r1.headers["cache-control"] == "public, max-age=30, stale-while-revalidate=60"
    body = r1.json()
    assert body["carrier"] == "maxis"
    assert body["min_samples"] == 5
    assert len(body["cells"]) == 1
    assert body["cells"][0]["confidence"] == "HIGH"
    assert fake_db.heatmap_queries == 1

    # Второй запрос с тем же ключом отдаётся из RAM-кэша (без обращения к БД)
    r2 = client.get("/v1/heatmap?carrier=maxis&window=24h&res=6")
    assert r2.status_code == 200
    assert fake_db.heatmap_queries == 1

    bad_carrier = client.get("/v1/heatmap?carrier=nonexistent")
    assert bad_carrier.status_code == 400

    inc = client.get("/v1/incidents/active")
    assert inc.status_code == 200
    items = inc.json()["active_incidents"]
    assert len(items) == 1
    assert items[0]["carrier_slug"] == "unifi"
    assert items[0]["avg_stall_ratio"] == 0.28


def test_origin_secret_middleware_and_local_node_failover():
    cfg = Settings(
        environment="test",
        node_secrets_json='{"1":"my_new_secret","2":"eu_new_secret"}',
        origin_secret="super-cf-secret",
        turnstile_enabled=False,
        enable_scheduler=False,
        seed_districts_on_startup=False,
    )
    fake_db = FakeDatabase()
    app = create_app(
        settings=cfg,
        db=fake_db,
        geo=FakeGeoIPService(),
        alerter=FakeAlerter(),
        turnstile=FakeTurnstile(True),
    )
    with TestClient(app) as c:
        # /healthz доступен без X-Origin-Secret, а /v1/* заблокирован
        assert c.get("/healthz").status_code == 200
        assert c.get("/v1/incidents/active").status_code == 403
        assert (
            c.get("/v1/incidents/active", headers={"X-Origin-Secret": "super-cf-secret"}).status_code
            == 200
        )

        # Имитируем падение MY-KUL (3 промаха /healthz -> исключение из плана)
        registry = app.state.state.node_registry
        for _ in range(3):
            registry.record_failure(1)
        assert registry.is_node_healthy(1) is False

        init_r = c.post(
            "/v1/tests/init",
            json={},
            headers={"CF-Connecting-IP": "103.5.180.10", "X-Origin-Secret": "super-cf-secret"},
        )
        assert init_r.status_code == 200
        plan = init_r.json()["probe_plan"]
        assert [p["node_id"] for p in plan] == [2]

        eu_receipt = _signed_receipt(
            secret="eu_new_secret",
            test_id=init_r.json()["test_id"],
            node_id=2,
            probe_mode="PULSE_ONLY",
            rtt_p50_ms=160.0,
            rtt_p95_ms=180.0,
            dl_kbps=None,
            ul_kbps=None,
        )
        comp = c.post(
            "/v1/tests/complete",
            json={
                "test_id": init_r.json()["test_id"],
                "receipts": [eu_receipt],
                "failed_nodes": [],
            },
            headers={"X-Origin-Secret": "super-cf-secret"},
        )
        assert comp.status_code == 200
        assert comp.json()["local_probe_unavailable"] is True
        assert comp.json()["verdict_code"] == "LOCAL_PROBE_UNAVAILABLE"