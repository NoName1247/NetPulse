from central.analytics import (
    compute_health_score,
    compute_raw_score,
    confidence_for_samples,
    evaluate_verdict,
    status_for_health_score,
)
from central.models import (
    DistrictStats,
    FailedNodeIn,
    NetClass,
    ReceiptIn,
    ReceiptMetrics,
    VerdictCode,
)


def _receipt(
    *,
    node_id: int = 1,
    probe_mode: str = "FULL_PROBE",
    rtt_p50: float = 25.0,
    rtt_p95: float = 40.0,
    stall: float = 0.0,
    dl_kbps: int | None = 15000,
    ul_kbps: int | None = 5000,
) -> ReceiptIn:
    return ReceiptIn(
        node_id=node_id,
        probe_mode=probe_mode,  # type: ignore[arg-type]
        measured_at=1764087005,
        metrics=ReceiptMetrics(
            rtt_p50_ms=rtt_p50,
            rtt_p95_ms=rtt_p95,
            jitter_ms=3.0,
            stall_ratio=stall,
            dl_kbps=dl_kbps,
            ul_kbps=ul_kbps,
        ),
        signature_hmac="a" * 64,
    )


def test_health_score_math_and_badges():
    raw_perfect = compute_raw_score(0.0, 30.0, 20000.0)
    assert raw_perfect == 100.0
    assert compute_health_score(raw_perfect, 15) == 88  # (15*100 + 15*75)/30 = 87.5 -> 88

    # NULL dl_kbps (PULSE_ONLY) использует фолбэк 5000 kbps -> штраф (10000-5000)/500 = 10
    raw_pulse = compute_raw_score(0.0, 40.0, None)
    assert raw_pulse == 90.0

    assert confidence_for_samples(5) == "LOW"
    assert confidence_for_samples(15) == "MED"
    assert confidence_for_samples(50) == "HIGH"

    assert status_for_health_score(80) == "HEALTHY"
    assert status_for_health_score(60) == "DEGRADED"
    assert status_for_health_score(40) == "MAJOR_OUTAGE"


def test_verdict_vpn_active_takes_priority():
    res = evaluate_verdict(
        my_receipt=_receipt(),
        eu_receipt=_receipt(node_id=2, probe_mode="PULSE_ONLY", dl_kbps=None, ul_kbps=None),
        failed_nodes=[],
        local_node_ids={1},
        district_stats=None,
        net_class=NetClass.VPN_DATACENTER,
        carrier_name="Cloudflare WARP",
        district_name="Bukit Jalil",
    )
    assert res.verdict_code == VerdictCode.VPN_ACTIVE
    assert "VPN/Proxy" in res.verdict_text_ru
    assert "VPN/Proxy" in res.verdict_text_en
    assert "VPN/Proksi" in res.verdict_text_bm


def test_verdict_intl_route_degraded_on_eu_timeout():
    res = evaluate_verdict(
        my_receipt=_receipt(rtt_p50=28.4),
        eu_receipt=None,
        failed_nodes=[FailedNodeIn(node_id=2, error="TIMEOUT")],
        local_node_ids={1},
        district_stats=DistrictStats(20, 0.01, 40.0, 15000.0, 88, "HEALTHY", 40),
        net_class=NetClass.MOBILE_CARRIER,
        carrier_name="Maxis / Hotlink",
        district_name="Bukit Jalil",
    )
    assert res.verdict_code == VerdictCode.INTL_ROUTE_DEGRADED
    assert "28.4 ms" in res.verdict_text_ru


def test_verdict_district_carrier_outage_and_partial_degradation():
    bad_my = _receipt(rtt_p95=240.0, stall=0.3, dl_kbps=800)
    res_major = evaluate_verdict(
        my_receipt=bad_my,
        eu_receipt=None,
        failed_nodes=[],
        local_node_ids={1},
        district_stats=DistrictStats(32, 0.30, 250.0, 900.0, 35, "MAJOR_OUTAGE", 50),
        net_class=NetClass.FIXED_BROADBAND,
        carrier_name="Unifi",
        district_name="Cheras",
    )
    assert res_major.verdict_code == VerdictCode.DISTRICT_CARRIER_OUTAGE
    assert "32" in res_major.verdict_text_ru

    # Серая зона 50 <= district_health < 75 при N >= 15
    res_mid = evaluate_verdict(
        my_receipt=bad_my,
        eu_receipt=None,
        failed_nodes=[],
        local_node_ids={1},
        district_stats=DistrictStats(18, 0.12, 170.0, 4000.0, 62, "DEGRADED", 40),
        net_class=NetClass.FIXED_BROADBAND,
        carrier_name="Unifi",
        district_name="Cheras",
    )
    assert res_mid.verdict_code == VerdictCode.DISTRICT_CARRIER_OUTAGE


def test_verdict_local_device_vs_throttling():
    healthy_district = DistrictStats(25, 0.01, 40.0, 18000.0, 90, "HEALTHY", 60)

    # Только шейпинг dl_kbps < 1500 при стабильном пинге
    throttled_my = _receipt(rtt_p50=25.0, rtt_p95=45.0, stall=0.0, dl_kbps=900)
    res_throttle = evaluate_verdict(
        my_receipt=throttled_my,
        eu_receipt=None,
        failed_nodes=[],
        local_node_ids={1},
        district_stats=healthy_district,
        net_class=NetClass.MOBILE_CARRIER,
        carrier_name="Maxis",
        district_name="KLCC",
    )
    assert res_throttle.verdict_code == VerdictCode.LOCAL_DEVICE_OR_INDOOR
    assert "шейпинг" in res_throttle.verdict_text_ru

    # Высокий пинг/потери при здоровом районе
    bad_ping_my = _receipt(rtt_p95=220.0, stall=0.25, dl_kbps=10000)
    res_local = evaluate_verdict(
        my_receipt=bad_ping_my,
        eu_receipt=None,
        failed_nodes=[],
        local_node_ids={1},
        district_stats=healthy_district,
        net_class=NetClass.MOBILE_CARRIER,
        carrier_name="Maxis",
        district_name="KLCC",
    )
    assert res_local.verdict_code == VerdictCode.LOCAL_DEVICE_OR_INDOOR
    assert "У других пользователей" in res_local.verdict_text_ru


def test_verdict_low_sample_and_healthy_and_local_unavailable():
    bad_my = _receipt(rtt_p95=210.0, stall=0.2, dl_kbps=1000)
    res_low = evaluate_verdict(
        my_receipt=bad_my,
        eu_receipt=None,
        failed_nodes=[],
        local_node_ids={1},
        district_stats=DistrictStats(4, 0.2, 200.0, 1000.0, 65, "DEGRADED", 4),
        net_class=NetClass.MOBILE_CARRIER,
        carrier_name="Maxis",
        district_name="Kepong",
    )
    assert res_low.verdict_code == VerdictCode.DEGRADED_LOW_SAMPLE
    assert "(4/15)" in res_low.verdict_text_ru

    res_ok = evaluate_verdict(
        my_receipt=_receipt(),
        eu_receipt=_receipt(node_id=2, probe_mode="PULSE_ONLY", rtt_p50=155.0, dl_kbps=None, ul_kbps=None),
        failed_nodes=[],
        local_node_ids={1},
        district_stats=DistrictStats(20, 0.01, 40.0, 15000.0, 90, "HEALTHY", 40),
        net_class=NetClass.MOBILE_CARRIER,
        carrier_name="Maxis",
        district_name="KLCC",
    )
    assert res_ok.verdict_code == VerdictCode.NETWORK_HEALTHY

    res_unavail = evaluate_verdict(
        my_receipt=None,
        eu_receipt=_receipt(node_id=2, probe_mode="PULSE_ONLY", rtt_p50=162.0, dl_kbps=None, ul_kbps=None),
        failed_nodes=[],
        local_node_ids=set(),
        district_stats=None,
        net_class=NetClass.MOBILE_CARRIER,
        carrier_name="Maxis",
        district_name="KLCC",
        local_probe_unavailable=True,
    )
    assert res_unavail.verdict_code == VerdictCode.LOCAL_PROBE_UNAVAILABLE