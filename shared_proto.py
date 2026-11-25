import hashlib
import hmac
import time

# Бинарные фреймы используются ТОЛЬКО для 3B Ping/Pong и 64KB чанков данных.
# Все управляющие сообщения (hello, dl_mark, dl_ack, ul_go, ul_end, receipt) — текстовый JSON!
OP_PING = 0x01  # Node -> Client: [0x01][seq:uint16]
OP_PONG = 0x02  # Client -> Node: [0x02][seq:uint16]

PING_COUNT = 12
PING_INTERVAL_S = 0.15    # 12 * 0.15s = 1.8s окно отправки + 1.2s хвост = <= 3.0s для MY и EU
PING_TIMEOUT_S = 1.5
PING_PHASE_BUDGET_S = 3.2
EARLY_ABORT_MISSES = 5

CAL_DL_BYTES = 256 * 1024       # Этап 2A: калибровка 256 KB (корректно меряет от 0.25 Mbps!)
MAX_DL_BYTES = 2 * 1024 * 1024  # Этап 2B: потолок 2 MB для быстрых каналов (4G/5G)
MAX_UL_BYTES = 1 * 1024 * 1024  # Потолок 1 MB для Upload
SLOW_UL_BYTES = 256 * 1024      # Лимит Upload при медленном канале (< 2 Mbps)


def parse_secrets(raw: str) -> list[str]:
    """Поддержка ротации без даунтайма: 'new_key,old_key'. Подпись всегда первым, проверка обоими."""
    return [s.strip() for s in raw.split(",") if s.strip()]


def _hmac_hex(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def sign_probe_token(
    secrets: list[str], test_id: str, node_id: int, mode: str, ttl_s: int = 60
) -> str:
    exp = int(time.time()) + ttl_s
    payload = f"{test_id}|{node_id}|{mode}|{exp}"
    return f"{payload}|{_hmac_hex(secrets[0], payload)}"


def verify_probe_token(
    secrets: list[str], token: str, expected_node_id: int
) -> tuple[str, str] | None:
    parts = token.split("|")
    if len(parts) != 5:
        return None
    test_id, node_id_s, mode, exp_s, sig = parts
    try:
        if int(node_id_s) != expected_node_id or int(exp_s) < int(time.time()):
            return None
    except ValueError:
        return None
    if mode not in ("FULL_PROBE", "PULSE_ONLY"):
        return None
    payload = f"{test_id}|{node_id_s}|{mode}|{exp_s}"
    if any(hmac.compare_digest(_hmac_hex(sec, payload), sig) for sec in secrets):
        return test_id, mode
    return None


def canonical_receipt_payload(
    test_id: str,
    node_id: int,
    actual_mode: str,
    measured_at: int,
    rtt_p50_ms: float,
    rtt_p95_ms: float,
    jitter_ms: float,
    stall_ratio: float,
    dl_kbps: int | None,
    ul_kbps: int | None,
) -> str:
    # None сериализуется как "N", чтобы отличать PULSE_ONLY (NULL в БД) от реального 0 kbps!
    dl_s = "N" if dl_kbps is None else str(int(dl_kbps))
    ul_s = "N" if ul_kbps is None else str(int(ul_kbps))
    return (
        f"{test_id}|{node_id}|{actual_mode}|{measured_at}|"
        f"{round(rtt_p50_ms * 10)}|{round(rtt_p95_ms * 10)}|{round(jitter_ms * 10)}|"
        f"{round(stall_ratio * 10000)}|{dl_s}|{ul_s}"
    )


def sign_receipt(secrets: list[str], **kwargs) -> str:
    return _hmac_hex(secrets[0], canonical_receipt_payload(**kwargs))


def verify_receipt(secrets: list[str], signature: str, **kwargs) -> bool:
    payload = canonical_receipt_payload(**kwargs)
    return any(hmac.compare_digest(_hmac_hex(sec, payload), signature) for sec in secrets)