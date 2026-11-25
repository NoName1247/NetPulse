import os
import struct

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import shared_proto as proto

os.environ.setdefault("NODE_ID", "1")
os.environ.setdefault("NODE_SECRET", "probe_new,probe_old")

import probe.main as probe_main  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_probe_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(probe_main, "NODE_ID", 1)
    monkeypatch.setattr(probe_main, "NODE_SECRETS", ["probe_new", "probe_old"])
    monkeypatch.setattr(probe_main, "MAX_FULL_SESSIONS", 50)
    monkeypatch.setattr(probe_main, "active_sessions", 0)
    probe_main.seen_test_ids.clear()

    # Ускоряем фазу ping для быстрых CI-тестов, сохраняя протокол 1-в-1
    monkeypatch.setattr(proto, "PING_COUNT", 4)
    monkeypatch.setattr(proto, "PING_INTERVAL_S", 0.005)
    monkeypatch.setattr(proto, "PING_TIMEOUT_S", 0.5)
    monkeypatch.setattr(proto, "PING_PHASE_BUDGET_S", 1.0)
    yield
    probe_main.seen_test_ids.clear()


def test_probe_healthz():
    client = TestClient(probe_main.app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["node_id"] == 1
    assert body["mode"] == "FULL_PROBE"
    assert isinstance(body["server_time"], int)


def test_probe_ws_rejects_invalid_token_and_replay_test_id():
    client = TestClient(probe_main.app)

    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/probe/ws?token=invalid"):
        pass

    token = proto.sign_probe_token(["probe_new"], "dup-test-1", node_id=1, mode="PULSE_ONLY")
    # Предварительно регистрируем test_id -> повторное подключение должно быть отклонено
    assert probe_main._claim_test_id("dup-test-1") is True
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(f"/probe/ws?token={token}"):
        pass


def test_probe_ws_full_probe_happy_path():
    client = TestClient(probe_main.app)
    token = proto.sign_probe_token(["probe_old"], "test-full-1", node_id=1, mode="FULL_PROBE")

    receipt = None
    with client.websocket_connect(f"/probe/ws?token={token}") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["actual_mode"] == "FULL_PROBE"

        stage_bytes = 0
        while True:
            msg = ws.receive()
            if msg["type"] == "websocket.close":
                break
            if msg.get("bytes") is not None:
                raw = msg["bytes"]
                if len(raw) == 3 and raw[0] == proto.OP_PING:
                    _, seq = struct.unpack("!BH", raw)
                    ws.send_bytes(struct.pack("!BH", proto.OP_PONG, seq))
                else:
                    stage_bytes += len(raw)
            elif msg.get("text") is not None:
                import json

                data = json.loads(msg["text"])
                if data["type"] == "dl_mark":
                    ws.send_json({"type": "dl_ack", "stage": data["stage"], "bytes": stage_bytes})
                    stage_bytes = 0
                elif data["type"] == "ul_go":
                    ws.send_bytes(os.urandom(65536))
                    ws.send_bytes(os.urandom(65536))
                    ws.send_json({"type": "ul_end"})
                elif data["type"] == "receipt":
                    receipt = data
                    break

    assert receipt is not None
    assert receipt["probe_mode"] == "FULL_PROBE"
    assert receipt["metrics"]["dl_kbps"] is not None
    assert receipt["metrics"]["ul_kbps"] is not None
    assert receipt["metrics"]["stall_ratio"] == 0.0
    assert proto.verify_receipt(
        ["probe_new", "probe_old"],
        receipt["signature_hmac"],
        test_id="test-full-1",
        node_id=1,
        actual_mode=receipt["probe_mode"],
        measured_at=receipt["measured_at"],
        **receipt["metrics"],
    )
    assert probe_main.active_sessions == 0


def test_probe_ws_load_shedding_forces_pulse_only_with_null_throughput(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(probe_main, "MAX_FULL_SESSIONS", 1)
    monkeypatch.setattr(probe_main, "active_sessions", 1)

    client = TestClient(probe_main.app)
    token = proto.sign_probe_token(["probe_new"], "test-shed-1", node_id=1, mode="FULL_PROBE")

    receipt = None
    with client.websocket_connect(f"/probe/ws?token={token}") as ws:
        hello = ws.receive_json()
        assert hello["actual_mode"] == "PULSE_ONLY"

        stage_bytes = 0
        while True:
            msg = ws.receive()
            if msg["type"] == "websocket.close":
                break
            if msg.get("bytes") is not None:
                raw = msg["bytes"]
                if len(raw) == 3 and raw[0] == proto.OP_PING:
                    _, seq = struct.unpack("!BH", raw)
                    ws.send_bytes(struct.pack("!BH", proto.OP_PONG, seq))
                else:
                    stage_bytes += len(raw)
            elif msg.get("text") is not None:
                import json

                data = json.loads(msg["text"])
                if data["type"] == "dl_mark":
                    ws.send_json({"type": "dl_ack", "stage": data["stage"], "bytes": stage_bytes})
                    stage_bytes = 0
                elif data["type"] == "receipt":
                    receipt = data
                    break

    assert receipt is not None
    assert receipt["probe_mode"] == "PULSE_ONLY"
    assert receipt["metrics"]["dl_kbps"] is None
    assert receipt["metrics"]["ul_kbps"] is None