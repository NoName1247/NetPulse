/**
 * Гибридный WSS-клиент замера сетевых метрик (соответствует shared_proto.py и probe/main.py).
 * - Бинарные фреймы: 3B Ping/Pong ([0x01/0x02][seq:uint16 BE]) и 64KB несжимаемые чанки.
 * - Текстовые JSON-фреймы: hello, dl_mark, dl_ack, ul_go, ul_end, receipt.
 */

const OP_PING = 0x01;
const OP_PONG = 0x02;
const CHUNK_SIZE = 65536;
const MAX_WS_BUFFERED = 262144;

let cachedRandomChunk = null;

function getRandomChunk64K() {
    if (cachedRandomChunk) {
        return cachedRandomChunk;
    }
    const buf = new Uint8Array(CHUNK_SIZE);
    // Заполняем по 16 КБ для совместимости со всеми версиями WebKit/Safari
    for (let offset = 0; offset < CHUNK_SIZE; offset += 16384) {
        crypto.getRandomValues(buf.subarray(offset, offset + 16384));
    }
    cachedRandomChunk = buf;
    return buf;
}

function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

async function runBoundedUpload(ws, maxBytes, maxMs) {
    const chunk = getRandomChunk64K();
    const startedAt = performance.now();
    let sent = 0;

    while (
        ws.readyState === WebSocket.OPEN &&
        sent < maxBytes &&
        performance.now() - startedAt < maxMs
    ) {
        if (ws.bufferedAmount > MAX_WS_BUFFERED) {
            await sleep(5);
            continue;
        }
        const remaining = maxBytes - sent;
        if (remaining >= CHUNK_SIZE) {
            ws.send(chunk);
            sent += CHUNK_SIZE;
        } else {
            ws.send(chunk.subarray(0, remaining));
            sent += remaining;
        }
    }

    if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "ul_end" }));
    }
}

/**
 * Выполняет замер к одной Probe-ноде и возвращает:
 * - { ok: true, receipt } при получении подписанного чека
 * - { ok: false, failed: { node_id, error } } при таймауте/обрыве
 */
export function runSingleNodeProbe(planItem, onProgress) {
    return new Promise((resolve) => {
        const isPulse = planItem.probe_mode === "PULSE_ONLY";
        const connectTimeoutMs = isPulse ? 3000 : 4500;
        const totalTimeoutMs = isPulse ? 9000 : 22000;

        let settled = false;
        let opened = false;
        let stageBytesReceived = 0;
        let pingsAnswered = 0;

        const url = `${planItem.wss_url}?token=${encodeURIComponent(planItem.probe_token)}`;
        let ws;

        const finish = (outcome) => {
            if (settled) return;
            settled = true;
            clearTimeout(connectTimer);
            clearTimeout(totalTimer);
            if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
                try {
                    ws.close(1000);
                } catch {
                    // ignore close errors
                }
            }
            resolve(outcome);
        };

        const connectTimer = setTimeout(() => {
            if (!opened) {
                finish({ ok: false, failed: { node_id: planItem.node_id, error: "TIMEOUT" } });
            }
        }, connectTimeoutMs);

        const totalTimer = setTimeout(() => {
            finish({ ok: false, failed: { node_id: planItem.node_id, error: "TIMEOUT" } });
        }, totalTimeoutMs);

        try {
            ws = new WebSocket(url);
            ws.binaryType = "arraybuffer";
        } catch {
            finish({ ok: false, failed: { node_id: planItem.node_id, error: "UNREACHABLE" } });
            return;
        }

        ws.onopen = () => {
            opened = true;
            clearTimeout(connectTimer);
            onProgress?.({ node_id: planItem.node_id, region: planItem.region, phase: "connected" });
        };

        ws.onerror = () => {
            finish({
                ok: false,
                failed: { node_id: planItem.node_id, error: opened ? "DISCONNECTED" : "UNREACHABLE" },
            });
        };

        ws.onclose = (ev) => {
            if (!settled) {
                const errCode = ev.code === 4403 || ev.code === 4409 ? "REJECTED" : "DISCONNECTED";
                finish({ ok: false, failed: { node_id: planItem.node_id, error: errCode } });
            }
        };

        ws.onmessage = (event) => {
            if (settled) return;

            if (event.data instanceof ArrayBuffer) {
                const buf = event.data;
                if (buf.byteLength === 3) {
                    const view = new DataView(buf);
                    if (view.getUint8(0) === OP_PING) {
                        const seq = view.getUint16(1, false);
                        const pong = new Uint8Array([OP_PONG, (seq >> 8) & 0xff, seq & 0xff]);
                        if (ws.readyState === WebSocket.OPEN) {
                            ws.send(pong);
                        }
                        pingsAnswered += 1;
                        onProgress?.({
                            node_id: planItem.node_id,
                            region: planItem.region,
                            phase: "ping",
                            pingsAnswered,
                        });
                        return;
                    }
                }
                stageBytesReceived += buf.byteLength;
                return;
            }

            if (typeof event.data === "string") {
                let msg;
                try {
                    msg = JSON.parse(event.data);
                } catch {
                    return;
                }
                if (!msg || typeof msg !== "object") return;

                switch (msg.type) {
                    case "hello":
                        onProgress?.({
                            node_id: planItem.node_id,
                            region: planItem.region,
                            phase: "ping",
                            actual_mode: msg.actual_mode,
                        });
                        break;

                    case "dl_mark": {
                        const received = stageBytesReceived;
                        stageBytesReceived = 0;
                        if (ws.readyState === WebSocket.OPEN) {
                            ws.send(
                                JSON.stringify({
                                    type: "dl_ack",
                                    stage: msg.stage,
                                    bytes: received,
                                })
                            );
                        }
                        onProgress?.({
                            node_id: planItem.node_id,
                            region: planItem.region,
                            phase: "download",
                            stage: msg.stage,
                        });
                        break;
                    }

                    case "ul_go": {
                        onProgress?.({ node_id: planItem.node_id, region: planItem.region, phase: "upload" });
                        const maxBytes = Number(msg.max_bytes) || 1048576;
                        const maxMs = Number(msg.max_ms) || 1500;
                        runBoundedUpload(ws, maxBytes, maxMs).catch(() => {
                            // Ошибки сокета обработает onclose/onerror
                        });
                        break;
                    }

                    case "receipt":
                        onProgress?.({ node_id: planItem.node_id, region: planItem.region, phase: "done" });
                        finish({
                            ok: true,
                            receipt: {
                                node_id: msg.node_id,
                                probe_mode: msg.probe_mode,
                                measured_at: msg.measured_at,
                                metrics: msg.metrics,
                                signature_hmac: msg.signature_hmac,
                            },
                        });
                        break;

                    default:
                        break;
                }
            }
        };
    });
}

/**
 * Запускает замеры ко всем нодам плана параллельно (MY + EU).
 */
export async function runProbePlan(probePlan, onProgress) {
    const outcomes = await Promise.all(
        probePlan.map((item) => runSingleNodeProbe(item, onProgress))
    );

    const receipts = [];
    const failed_nodes = [];
    for (const outcome of outcomes) {
        if (outcome.ok) {
            receipts.push(outcome.receipt);
        } else {
            failed_nodes.push(outcome.failed);
        }
    }
    return { receipts, failed_nodes };
}