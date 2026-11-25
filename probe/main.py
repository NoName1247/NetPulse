import asyncio
import json
import logging
import os
import statistics
import struct
import time

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

import shared_proto as proto

logger = logging.getLogger("probe_daemon")

NODE_ID = int(os.environ["NODE_ID"])
NODE_SECRETS = proto.parse_secrets(os.environ["NODE_SECRET"])  # поддерживает "new,old"
if not NODE_SECRETS:
    raise RuntimeError("NODE_SECRET environment variable must contain at least one secret")
MAX_FULL_SESSIONS = int(os.environ.get("MAX_FULL_SESSIONS", "50"))

# Несжимаемый случайный буфер 64 КБ (защита от ложного сжатия Brotli в Caddy)
CHUNK_64K = os.urandom(65536)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
active_sessions = 0
seen_test_ids: dict[str, float] = {}  # Анти-DoS против повторного использования токена (TTL 65s)


def current_node_mode() -> str:
    return "PULSE_ONLY" if active_sessions >= MAX_FULL_SESSIONS else "FULL_PROBE"


def _claim_test_id(test_id: str) -> bool:
    now = time.monotonic()
    if len(seen_test_ids) > 500:
        expired = [k for k, exp in seen_test_ids.items() if exp < now]
        for k in expired:
            seen_test_ids.pop(k, None)
    if test_id in seen_test_ids and seen_test_ids[test_id] >= now:
        return False
    seen_test_ids[test_id] = now + 65.0
    return True


def _safe_non_negative_int(value: object) -> int:
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


@app.get("/healthz")
async def healthz() -> dict[str, object]:
    return {
        "status": "ok",
        "node_id": NODE_ID,
        "mode": current_node_mode(),
        "active_sessions": active_sessions,
        "server_time": int(time.time()),  # Контроль Clock Skew (>5s -> алерт на Central)
    }


async def _recv_json_until(ws: WebSocket, expected_type: str, deadline: float) -> dict | None:
    # Чтение управляющего JSON-фрейма с жёстким внешним дедлайном (защита от Slowloris)
    while True:
        rem = deadline - time.monotonic()
        if rem <= 0:
            return None
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=rem)
        except TimeoutError:
            return None
        if msg["type"] == "websocket.disconnect":
            raise WebSocketDisconnect()
        if msg.get("text"):
            try:
                data = json.loads(msg["text"])
                if isinstance(data, dict) and data.get("type") == expected_type:
                    return data
            except json.JSONDecodeError:
                continue


async def _run_pipelined_pings(ws: WebSocket) -> tuple[float, float, float, float, bool]:
    # Асинхронный pipelined Ping (<= 3.2s для MY и EU) + Early Abort + жёсткий дедлайн
    sent_ts: dict[int, int] = {}
    rtts_ms: list[float] = []
    aborted = False
    deadline = time.monotonic() + proto.PING_PHASE_BUDGET_S

    async def sender() -> None:
        for seq in range(proto.PING_COUNT):
            if aborted or time.monotonic() >= deadline - 0.5:
                break
            sent_ts[seq] = time.monotonic_ns()
            await ws.send_bytes(struct.pack("!BH", proto.OP_PING, seq))
            await asyncio.sleep(proto.PING_INTERVAL_S)

    async def receiver() -> None:
        nonlocal aborted
        while len(rtts_ms) < proto.PING_COUNT:
            rem = deadline - time.monotonic()
            if rem <= 0:
                break
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=min(rem, 0.3))
            except TimeoutError:
                now_ns = time.monotonic_ns()
                timed_out = sum(
                    1
                    for t0 in sent_ts.values()
                    if (now_ns - t0) > proto.PING_TIMEOUT_S * 1e9
                )
                if not rtts_ms and timed_out >= proto.EARLY_ABORT_MISSES:
                    aborted = True
                    break
                continue
            if msg["type"] == "websocket.disconnect":
                raise WebSocketDisconnect()
            raw = msg.get("bytes")
            if raw and len(raw) == 3 and raw[0] == proto.OP_PONG:
                _, seq = struct.unpack("!BH", raw)
                t0 = sent_ts.pop(seq, None)
                if t0 is not None:
                    rtt = (time.monotonic_ns() - t0) / 1_000_000.0
                    if rtt <= proto.PING_TIMEOUT_S * 1000:
                        rtts_ms.append(rtt)

    send_task = asyncio.create_task(sender())
    try:
        await receiver()
    finally:
        send_task.cancel()
        await asyncio.gather(send_task, return_exceptions=True)

    total_sent = max(1, len(rtts_ms) + len(sent_ts))
    stall_ratio = round(1.0 - (len(rtts_ms) / total_sent), 4)
    if not rtts_ms:
        return 1500.0, 1500.0, 0.0, 1.0, True
    rtt_p50 = round(statistics.median(rtts_ms), 1)
    rtt_p95 = round(sorted(rtts_ms)[max(0, int(len(rtts_ms) * 0.95) - 1)], 1)
    diffs = [abs(rtts_ms[i] - rtts_ms[i - 1]) for i in range(1, len(rtts_ms))]
    jitter = round(statistics.mean(diffs), 1) if diffs else 0.0
    return rtt_p50, rtt_p95, jitter, stall_ratio, aborted


async def _run_adaptive_download(
    ws: WebSocket, actual_mode: str, rtt_p50_ms: float
) -> tuple[int | None, bool]:
    # 2-этапный адаптивный DL. Работает от 0.25 Mbps (3G/шейпинг) до 500+ Mbps (5G) без краша
    # Этап 2A: Калибровка 256 KB (таймаут 8s покрывает каналы до 0.25 Mbps)
    t0 = time.monotonic()
    for _ in range(proto.CAL_DL_BYTES // 65536):
        await ws.send_bytes(CHUNK_64K)
    await ws.send_json({"type": "dl_mark", "stage": 1, "bytes": proto.CAL_DL_BYTES})

    ack1 = await _recv_json_until(ws, "dl_ack", time.monotonic() + 8.0)
    t1 = time.monotonic()
    if ack1 is None:
        return (200 if actual_mode == "FULL_PROBE" else None), True

    if actual_mode == "PULSE_ONLY":
        return None, False

    elapsed1 = max(0.02, (t1 - t0) - 0.5 * (rtt_p50_ms / 1000.0))
    recv1 = min(_safe_non_negative_int(ack1.get("bytes", 0)), proto.CAL_DL_BYTES)
    est_kbps = int((recv1 * 8) / (elapsed1 * 1000))

    if elapsed1 >= 0.8 or est_kbps < 2500:
        return max(1, est_kbps), False

    # Этап 2B: Подбираем объём под ~1.2 сек (512 KB .. 2 MB) для точного замера 4G/5G
    chunks = max(8, min(proto.MAX_DL_BYTES // 65536, int((est_kbps * 1000 / 8 * 1.2) // 65536)))
    burst_bytes = chunks * 65536

    t2_start = time.monotonic()
    for _ in range(chunks):
        await ws.send_bytes(CHUNK_64K)
    await ws.send_json({"type": "dl_mark", "stage": 2, "bytes": burst_bytes})

    ack2 = await _recv_json_until(ws, "dl_ack", time.monotonic() + 5.0)
    t2_end = time.monotonic()
    if ack2 is None:
        return max(1, est_kbps), True

    elapsed2 = max(0.02, (t2_end - t2_start) - 0.5 * (rtt_p50_ms / 1000.0))
    recv2 = min(_safe_non_negative_int(ack2.get("bytes", 0)), burst_bytes)
    return max(1, int((recv2 * 8) / (elapsed2 * 1000))), False


async def _run_bounded_upload(ws: WebSocket, dl_kbps: int | None) -> int | None:
    ul_limit = (
        proto.SLOW_UL_BYTES
        if (dl_kbps is not None and dl_kbps < 2000)
        else proto.MAX_UL_BYTES
    )
    await ws.send_json({"type": "ul_go", "max_bytes": ul_limit, "max_ms": 1500})

    ul_bytes = 0
    t_start: float | None = None
    deadline = time.monotonic() + 4.0

    while ul_bytes < ul_limit:
        rem = deadline - time.monotonic()
        if rem <= 0:
            break
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=rem)
        except TimeoutError:
            break
        if msg["type"] == "websocket.disconnect":
            raise WebSocketDisconnect()
        if msg.get("text"):
            try:
                data = json.loads(msg["text"])
                if isinstance(data, dict) and data.get("type") == "ul_end":
                    break
            except json.JSONDecodeError:
                continue
        elif msg.get("bytes"):
            if t_start is None:
                t_start = time.monotonic()
            ul_bytes += len(msg["bytes"])

    if t_start is None or ul_bytes == 0:
        return None
    elapsed = max(0.05, time.monotonic() - t_start)
    return int((min(ul_bytes, ul_limit) * 8) / (elapsed * 1000))


@app.websocket("/probe/ws")
async def probe_ws(ws: WebSocket, token: str = "") -> None:
    global active_sessions

    verified = proto.verify_probe_token(NODE_SECRETS, token, expected_node_id=NODE_ID)
    if not verified:
        await ws.close(code=4403)
        return

    test_id, requested_mode = verified
    if not _claim_test_id(test_id):
        await ws.close(code=4409)
        return

    await ws.accept()
    active_sessions += 1

    actual_mode = (
        "PULSE_ONLY"
        if (requested_mode == "PULSE_ONLY" or current_node_mode() == "PULSE_ONLY")
        else "FULL_PROBE"
    )

    try:
        await ws.send_json(
            {
                "type": "hello",
                "node_id": NODE_ID,
                "actual_mode": actual_mode,
                "ping_count": proto.PING_COUNT,
            }
        )

        rtt_p50, rtt_p95, jitter, stall_ratio, ping_aborted = await _run_pipelined_pings(ws)

        dl_kbps: int | None = None
        ul_kbps: int | None = None
        if not ping_aborted and stall_ratio < 0.8:
            dl_kbps, dl_timed_out = await _run_adaptive_download(ws, actual_mode, rtt_p50)
            if dl_timed_out:
                stall_ratio = round(max(stall_ratio, 0.5), 4)
            elif actual_mode == "FULL_PROBE":
                ul_kbps = await _run_bounded_upload(ws, dl_kbps)

        measured_at = int(time.time())
        sig = proto.sign_receipt(
            NODE_SECRETS,
            test_id=test_id,
            node_id=NODE_ID,
            actual_mode=actual_mode,
            measured_at=measured_at,
            rtt_p50_ms=rtt_p50,
            rtt_p95_ms=rtt_p95,
            jitter_ms=jitter,
            stall_ratio=stall_ratio,
            dl_kbps=dl_kbps,
            ul_kbps=ul_kbps,
        )
        await ws.send_json(
            {
                "type": "receipt",
                "node_id": NODE_ID,
                "probe_mode": actual_mode,
                "measured_at": measured_at,
                "metrics": {
                    "rtt_p50_ms": rtt_p50,
                    "rtt_p95_ms": rtt_p95,
                    "jitter_ms": jitter,
                    "stall_ratio": stall_ratio,
                    "dl_kbps": dl_kbps,
                    "ul_kbps": ul_kbps,
                },
                "signature_hmac": sig,
            }
        )
        await ws.close(code=1000)

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Unexpected error during probe session on node_id=%s", NODE_ID)
    finally:
        active_sessions -= 1