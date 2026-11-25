import time

import shared_proto as proto


def test_parse_secrets_supports_rotation():
    assert proto.parse_secrets(" new_key , old_key , ") == ["new_key", "old_key"]
    assert proto.parse_secrets("") == []


def test_probe_token_sign_verify_and_rotation():
    old_secrets = ["old_secret"]
    rotated_secrets = ["new_secret", "old_secret"]

    token = proto.sign_probe_token(old_secrets, "test-123", node_id=1, mode="FULL_PROBE", ttl_s=60)
    assert proto.verify_probe_token(rotated_secrets, token, expected_node_id=1) == (
        "test-123",
        "FULL_PROBE",
    )
    assert proto.verify_probe_token(rotated_secrets, token, expected_node_id=2) is None
    assert proto.verify_probe_token(["wrong"], token, expected_node_id=1) is None


def test_probe_token_rejects_expired_and_invalid_mode():
    secrets = ["sec1"]
    expired = proto.sign_probe_token(secrets, "test-exp", node_id=1, mode="PULSE_ONLY", ttl_s=-5)
    assert proto.verify_probe_token(secrets, expired, expected_node_id=1) is None

    bad_mode = proto.sign_probe_token(secrets, "test-bad", node_id=1, mode="ADMIN", ttl_s=60)
    assert proto.verify_probe_token(secrets, bad_mode, expected_node_id=1) is None


def test_canonical_receipt_distinguishes_none_from_zero():
    now = int(time.time())
    payload_null = proto.canonical_receipt_payload(
        test_id="t1",
        node_id=2,
        actual_mode="PULSE_ONLY",
        measured_at=now,
        rtt_p50_ms=155.26,
        rtt_p95_ms=170.04,
        jitter_ms=5.55,
        stall_ratio=0.0123,
        dl_kbps=None,
        ul_kbps=None,
    )
    payload_zero = proto.canonical_receipt_payload(
        test_id="t1",
        node_id=2,
        actual_mode="PULSE_ONLY",
        measured_at=now,
        rtt_p50_ms=155.26,
        rtt_p95_ms=170.04,
        jitter_ms=5.55,
        stall_ratio=0.0123,
        dl_kbps=0,
        ul_kbps=0,
    )
    assert payload_null.endswith("|N|N")
    assert payload_zero.endswith("|0|0")
    assert payload_null != payload_zero


def test_receipt_sign_verify_and_tamper_detection():
    secrets = ["new_key", "old_key"]
    kwargs = dict(
        test_id="019a9f4c-7b2a-7c10-9e44-1f8a2b3c4d5e",
        node_id=1,
        actual_mode="FULL_PROBE",
        measured_at=1764087005,
        rtt_p50_ms=28.4,
        rtt_p95_ms=42.1,
        jitter_ms=4.2,
        stall_ratio=0.0,
        dl_kbps=12500,
        ul_kbps=3200,
    )
    sig_old = proto.sign_receipt(["old_key"], **kwargs)
    assert proto.verify_receipt(secrets, sig_old, **kwargs) is True

    tampered = {**kwargs, "actual_mode": "PULSE_ONLY"}
    assert proto.verify_receipt(secrets, sig_old, **tampered) is False

    tampered_dl = {**kwargs, "dl_kbps": 100}
    assert proto.verify_receipt(secrets, sig_old, **tampered_dl) is False