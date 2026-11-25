import { runProbePlan } from "./probe-client.js";

const API_BASE = (
    window.NETPULSE_API_BASE ||
    document.querySelector('meta[name="api-base"]')?.content ||
    ""
).replace(/\/$/, "");

const TURNSTILE_SITEKEY =
    window.NETPULSE_TURNSTILE_SITEKEY ||
    document.querySelector('meta[name="turnstile-sitekey"]')?.content ||
    "";

const KL_CENTER = [3.1390, 101.6869];

const I18N = {
    en: {
        hero_title: "Check Your Malaysia Network & Outages",
        hero_subtitle:
            "5-second local (MY) + international (EU) probe. Zero accounts, zero IP or GPS storage (PDPA Privacy-by-Design).",
        gps_label: "Use district GPS (quantized to ~0.74 km² H3 cell, raw coordinates discarded)",
        btn_start: "Run Diagnostic Test",
        btn_running: "Testing...",
        btn_refresh: "Refresh",
        btn_fit_map: "Fit Map",
        m_local_rtt: "Local RTT (MY)",
        m_intl_rtt: "Intl RTT (EU)",
        m_dl: "Download",
        m_ul: "Upload",
        m_stall: "Stall Ratio",
        m_district: "District Health",
        incidents_title: "Active Outages in Malaysia",
        heatmap_title: "Coverage & Quality Elevation Map",
        all_carriers: "All Carriers",
        s_zones: "Zones",
        s_users: "Unique Users",
        s_quality: "Avg Quality",
        s_dl: "Median DL",
        s_rtt: "Avg P95 RTT",
        legend_bad: "0 Outage",
        legend_good: "100 Excellent",
        no_incidents: "No mass carrier outages detected across Malaysia right now.",
        no_heatmap: "Not enough samples yet (minimum {n} unique users per zone). Run a diagnostic test above to light up your area!",
        step_gps: "Reading approximate location...",
        step_init: "Initializing session & detecting carrier...",
        step_probe: "Running MY + EU WebSocket probes...",
        step_complete: "Verifying signed receipts & evaluating verdict...",
        vpn_warn: "VPN / Datacenter IP detected: test is shown for you, but excluded from public carrier maps.",
        err_rate_limit: "Rate limit reached. Please retry in {s}s.",
        err_turnstile: "Please complete the human verification challenge first.",
        footer_privacy:
            "Malaysia PDPA 2010 Privacy-by-Design: IP addresses are hashed with daily MYT salt and discarded; GPS is quantized to H3 Res 8.",
    },
    bm: {
        hero_title: "Semak Rangkaian & Gangguan Internet Malaysia",
        hero_subtitle:
            "Ujian 5 saat tempatan (MY) + antarabangsa (EU). Tanpa akaun, tanpa simpanan IP atau GPS (PDPA Privacy-by-Design).",
        gps_label: "Guna GPS daerah (dikuantum ke sel H3 ~0.74 km², koordinat asal dipadam)",
        btn_start: "Mula Ujian Diagnostik",
        btn_running: "Sedang menguji...",
        btn_refresh: "Muat Semula",
        btn_fit_map: "Pusatkan Peta",
        m_local_rtt: "Ping Tempatan (MY)",
        m_intl_rtt: "Ping Antarabangsa (EU)",
        m_dl: "Muat Turun",
        m_ul: "Muat Naik",
        m_stall: "Kadar Sangkut",
        m_district: "Kesihatan Daerah",
        incidents_title: "Gangguan Aktif di Malaysia",
        heatmap_title: "Peta Ketinggian Kualiti & Liputan",
        all_carriers: "Semua Syarikat Telko",
        s_zones: "Zon",
        s_users: "Pengguna Unik",
        s_quality: "Purata Kualiti",
        s_dl: "Median DL",
        s_rtt: "Purata P95 RTT",
        legend_bad: "0 Gangguan",
        legend_good: "100 Cemerlang",
        no_incidents: "Tiada gangguan besar dikesan di seluruh Malaysia buat masa ini.",
        no_heatmap: "Sampel belum mencukupi (minimum {n} pengguna unik setiap zon). Jalankan ujian diagnostik di atas untuk memaparkan kawasan anda!",
        step_gps: "Mendapatkan lokasi anggaran...",
        step_init: "Memulakan sesi & mengenal pasti pembekal...",
        step_probe: "Menjalankan ujian WebSocket MY + EU...",
        step_complete: "Mengesahkan resit bertandatangan & keputusan...",
        vpn_warn: "VPN / Pusat Data dikesan: keputusan dipaparkan untuk anda tetapi dikecualikan daripada peta awam.",
        err_rate_limit: "Had permintaan dicapai. Cuba lagi dalam {s}s.",
        err_turnstile: "Sila lengkapkan pengesahan keselamatan terlebih dahulu.",
        footer_privacy:
            "PDPA Malaysia 2010 Privacy-by-Design: Alamat IP dicincang dengan garam harian MYT dan dipadam; GPS dikuantum ke H3 Res 8.",
    },
    ru: {
        hero_title: "Проверка сети и сбоев операторов Малайзии",
        hero_subtitle:
            "5-секундный замер MY + EU. Без аккаунтов, без хранения IP и GPS (PDPA Privacy-by-Design).",
        gps_label: "Использовать GPS района (квантуется в гексагон H3 ~0.74 км², исходные координаты стираются)",
        btn_start: "Запустить диагностику",
        btn_running: "Идёт замер...",
        btn_refresh: "Обновить",
        btn_fit_map: "Показать всё",
        m_local_rtt: "Локальный пинг (MY)",
        m_intl_rtt: "Внешний пинг (EU)",
        m_dl: "Загрузка (DL)",
        m_ul: "Отдача (UL)",
        m_stall: "Потери / Stall",
        m_district: "Индекс района",
        incidents_title: "Активные сбои в Малайзии",
        heatmap_title: "Карта высот качества сети (Красный — Зелёный)",
        all_carriers: "Все операторы",
        s_zones: "Точки",
        s_users: "Пользователи",
        s_quality: "Ср. качество",
        s_dl: "Медиана DL",
        s_rtt: "Ср. P95 RTT",
        legend_bad: "0 Сбой",
        legend_good: "100 Отлично",
        no_incidents: "Массовых сбоев у операторов Малайзии сейчас не зафиксировано.",
        no_heatmap: "Пока недостаточно замеров (минимум {n} уникальных пользователей). Запустите диагностику выше, чтобы увидеть точку на карте!",
        step_gps: "Получение примерного местоположения...",
        step_init: "Инициализация теста и определение оператора...",
        step_probe: "Выполнение WebSocket-замеров к MY и EU...",
        step_complete: "Проверка подписанных чеков и расчёт вердикта...",
        vpn_warn: "Обнаружен VPN / Datacenter IP: результат показан вам, но исключён из карт операторов.",
        err_rate_limit: "Превышен лимит запросов. Повторите через {s} сек.",
        err_turnstile: "Пожалуйста, пройдите проверку Cloudflare Turnstile.",
        footer_privacy:
            "PDPA 2010 Privacy-by-Design: IP хэшируется с суточной солью MYT и не сохраняется; GPS квантуется в H3 Res 8.",
    },
};

const state = {
    lang: localStorage.getItem("netpulse_lang") || "en",
    turnstileToken: null,
    turnstileWidgetId: null,
    lastResult: null,
    running: false,
    cells: [],
    incidents: [],
    map: null,
    heatLayer: null,
    pointGroup: null,
    incidentGroup: null,
    hasAutoFitted: false,
};

const $ = (id) => document.getElementById(id);

function t(key) {
    return (I18N[state.lang] && I18N[state.lang][key]) || I18N.en[key] || key;
}

function escapeHtml(str) {
    return String(str ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
}

function generateIdempotencyKey() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/**
 * Яркая цветовая шкала качества 0..100:
 * 0 (Ярко-красный) -> 35 (Оранжевый) -> 60 (Жёлтый) -> 100 (Изумрудно-зелёный)
 */
function scoreToRgb(score) {
    const s = Math.max(0, Math.min(100, Number(score) || 0));
    const stops = [
        [0, [255, 36, 36]],
        [35, [255, 125, 18]],
        [60, [250, 204, 21]],
        [82, [74, 222, 128]],
        [100, [16, 185, 80]],
    ];
    for (let i = 1; i < stops.length; i += 1) {
        const [s1, c1] = stops[i - 1];
        const [s2, c2] = stops[i];
        if (s <= s2) {
            const k = (s - s1) / (s2 - s1);
            return [
                Math.round(c1[0] + (c2[0] - c1[0]) * k),
                Math.round(c1[1] + (c2[1] - c1[1]) * k),
                Math.round(c1[2] + (c2[2] - c1[2]) * k),
            ];
        }
    }
    return [16, 185, 80];
}

function scoreToCss(score, alpha = 1) {
    const [r, g, b] = scoreToRgb(score);
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function visualScoreForCell(cell) {
    return cell.raw_score != null ? cell.raw_score : cell.health_score;
}

function metersToPixels(map, lat, lon, meters) {
    const dLat = meters / 111320.0;
    const p1 = map.latLngToContainerPoint([lat, lon]);
    const p2 = map.latLngToContainerPoint([lat + dLat, lon]);
    return Math.hypot(p2.x - p1.x, p2.y - p1.y);
}

function initQualityMap() {
    if (state.map) return true;
    if (!window.L) return false;
    const container = $("quality-map");
    if (!container) return false;

    const L = window.L;
    const map = L.map(container, {
        center: KL_CENTER,
        zoom: 12,
        zoomControl: true,
    });

    const googleStreets = L.tileLayer("https://mt{s}.google.com/vt/lyrs=m&x={x}&y={y}&z={z}", {
        subdomains: ["0", "1", "2", "3"],
        maxZoom: 19,
        attribution: "Map data &copy; Google",
    });
    const googleHybrid = L.tileLayer("https://mt{s}.google.com/vt/lyrs=y&x={x}&y={y}&z={z}", {
        subdomains: ["0", "1", "2", "3"],
        maxZoom: 19,
        attribution: "Map data &copy; Google",
    });
    const googleTerrain = L.tileLayer("https://mt{s}.google.com/vt/lyrs=p&x={x}&y={y}&z={z}", {
        subdomains: ["0", "1", "2", "3"],
        maxZoom: 19,
        attribution: "Map data &copy; Google",
    });
    const osmLayer = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: "&copy; OpenStreetMap contributors",
    });

    googleStreets.addTo(map);
    L.control.layers(
        {
            "Google Maps": googleStreets,
            "Google Satellite": googleHybrid,
            "Google Terrain": googleTerrain,
            "OpenStreetMap": osmLayer,
        },
        null,
        { position: "topright" }
    ).addTo(map);

    const ElevationOverlay = L.Layer.extend({
        onAdd(m) {
            this._map = m;
            this._canvas = L.DomUtil.create("canvas", "netpulse-elevation-canvas");
            this._canvas.style.position = "absolute";
            this._canvas.style.top = "0";
            this._canvas.style.left = "0";
            this._canvas.style.pointerEvents = "none";
            m.getPanes().overlayPane.appendChild(this._canvas);
            m.on("move moveend zoomend viewreset resize", this._redraw, this);
            this._redraw();
        },
        onRemove(m) {
            m.off("move moveend zoomend viewreset resize", this._redraw, this);
            if (this._canvas && this._canvas.parentNode) {
                this._canvas.parentNode.removeChild(this._canvas);
            }
        },
        setCells(cells) {
            this._cells = cells || [];
            this._redraw();
        },
        _redraw() {
            const m = this._map;
            if (!m || !this._canvas) return;
            const size = m.getSize();
            if (!size.x || !size.y) return;

            const dpr = Math.min(window.devicePixelRatio || 1, 2);
            const topLeft = m.containerPointToLayerPoint([0, 0]);
            L.DomUtil.setPosition(this._canvas, topLeft);

            this._canvas.width = Math.max(1, Math.round(size.x * dpr));
            this._canvas.height = Math.max(1, Math.round(size.y * dpr));
            this._canvas.style.width = `${size.x}px`;
            this._canvas.style.height = `${size.y}px`;

            const ctx = this._canvas.getContext("2d");
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.clearRect(0, 0, size.x, size.y);

            if (!this._cells || !this._cells.length) return;

            const resVal = $("filter-res")?.value || "8";
            const baseMeters = resVal === "6" ? 3600 : 1400;

            for (const cell of this._cells) {
                if (cell.lat == null || cell.lon == null) continue;

                const cPt = m.latLngToContainerPoint([cell.lat, cell.lon]);
                const rawPx = metersToPixels(m, cell.lat, cell.lon, baseMeters);
                const radiusPx = Math.max(38, Math.min(165, rawPx));

                if (
                    cPt.x + radiusPx < 0 ||
                    cPt.y + radiusPx < 0 ||
                    cPt.x - radiusPx > size.x ||
                    cPt.y - radiusPx > size.y
                ) {
                    continue;
                }

                // Чем больше замеров/пользователей в точке, тем ярче и плотнее ядро круга
                const reports = Math.max(1, Number(cell.reports || cell.samples || 1));
                const intensity = Math.min(0.95, 0.56 + 0.14 * Math.log2(reports));

                const vScore = visualScoreForCell(cell);
                const [r, g, b] = scoreToRgb(vScore);

                const grad = ctx.createRadialGradient(cPt.x, cPt.y, 0, cPt.x, cPt.y, radiusPx);
                grad.addColorStop(0, `rgba(${r}, ${g}, ${b}, ${Math.min(0.98, intensity + 0.08).toFixed(3)})`);
                grad.addColorStop(0.18, `rgba(${r}, ${g}, ${b}, ${(intensity * 0.78).toFixed(3)})`);
                grad.addColorStop(0.45, `rgba(${r}, ${g}, ${b}, ${(intensity * 0.42).toFixed(3)})`);
                grad.addColorStop(0.75, `rgba(${r}, ${g}, ${b}, ${(intensity * 0.14).toFixed(3)})`);
                grad.addColorStop(1, `rgba(${r}, ${g}, ${b}, 0)`);

                ctx.fillStyle = grad;
                ctx.beginPath();
                ctx.arc(cPt.x, cPt.y, radiusPx, 0, Math.PI * 2);
                ctx.fill();
            }
        },
    });

    state.map = map;
    state.heatLayer = new ElevationOverlay();
    state.heatLayer.addTo(map);
    state.pointGroup = L.featureGroup().addTo(map);
    state.incidentGroup = L.featureGroup().addTo(map);

    setTimeout(() => {
        map.invalidateSize();
        state.heatLayer?._redraw();
    }, 150);
    return true;
}

function renderMapLayers({ fitToCells = false } = {}) {
    if (!initQualityMap()) return;
    const L = window.L;

    state.heatLayer.setCells(state.cells);
    state.pointGroup.clearLayers();
    state.incidentGroup.clearLayers();

    const validCoords = [];

    for (const c of state.cells) {
        if (c.lat == null || c.lon == null) continue;
        validCoords.push([c.lat, c.lon]);

        const vScore = visualScoreForCell(c);
        const reports = Math.max(1, Number(c.reports || c.samples || 1));
        const dlText = c.median_dl_kbps != null ? `${(c.median_dl_kbps / 1000).toFixed(1)} Mbps` : "PULSE";

        // Невидимая область клика/наведения поверх вспышки (без видимой точки)
        const hitZone = L.circleMarker([c.lat, c.lon], {
            radius: 20,
            stroke: false,
            fillColor: "#000000",
            fillOpacity: 0,
            opacity: 0,
        });

        hitZone.bindTooltip(
            `<strong>${escapeHtml(c.district)}</strong> · ${vScore}/100 (${c.samples} users, ${reports} tests)`,
            { direction: "top", offset: [0, -6] }
        );
        hitZone.bindPopup(
            `<div>
                <strong>${escapeHtml(c.district)}</strong><br/>
                Quality: <strong>${vScore}/100</strong> (Smoothed: ${c.health_score}/100 · ${escapeHtml(c.status)})<br/>
                Users: <strong>${c.samples}</strong> · Tests: <strong>${reports}</strong> (${escapeHtml(c.confidence)})<br/>
                Median DL: <strong>${escapeHtml(dlText)}</strong> · P95 RTT: <strong>${c.p95_rtt_ms} ms</strong><br/>
                Stall: <strong>${(c.avg_stall_ratio * 100).toFixed(1)}%</strong><br/>
                <span class="muted">H3: ${escapeHtml(c.h3)}</span>
            </div>`
        );
        hitZone.addTo(state.pointGroup);
    }

    for (const inc of state.incidents) {
        const matched = state.cells.find((c) => c.h3 === inc.h3_res6);
        if (!matched || matched.lat == null || matched.lon == null) continue;
        const marker = L.marker([matched.lat, matched.lon], {
            icon: L.divIcon({
                className: "outage-pulse-icon",
                html: '<div class="outage-pulse-dot"></div>',
                iconSize: [16, 16],
                iconAnchor: [8, 8],
            }),
        });
        marker.bindPopup(
            `<div>
                <strong>${escapeHtml(inc.carrier_name)}</strong> — ${escapeHtml(inc.district_name)}<br/>
                Severity: <strong>${escapeHtml(inc.severity)}</strong><br/>
                Reports: <strong>${inc.unique_reports}</strong> · P95 RTT: <strong>${inc.p95_rtt_ms} ms</strong><br/>
                Stall: <strong>${(inc.avg_stall_ratio * 100).toFixed(1)}%</strong>
            </div>`
        );
        marker.addTo(state.incidentGroup);
    }

    if ((fitToCells || !state.hasAutoFitted) && validCoords.length > 0) {
        if (validCoords.length === 1) {
            const zoom = ($("filter-res")?.value || "8") === "6" ? 11 : 13;
            state.map.setView(validCoords[0], zoom);
        } else {
            state.map.fitBounds(L.latLngBounds(validCoords), { padding: [60, 60], maxZoom: 13 });
        }
        state.hasAutoFitted = true;
    }

    state.heatLayer._redraw();
}

function fitMapToData() {
    if (!state.map) return;
    const coords = state.cells.filter((c) => c.lat != null && c.lon != null).map((c) => [c.lat, c.lon]);
    if (coords.length === 1) {
        const zoom = ($("filter-res")?.value || "8") === "6" ? 11 : 13;
        state.map.setView(coords[0], zoom);
    } else if (coords.length > 1) {
        state.map.fitBounds(window.L.latLngBounds(coords), { padding: [60, 60], maxZoom: 13 });
    } else {
        state.map.setView(KL_CENTER, 12);
    }
}

function applyLanguage(lang) {
    if (!I18N[lang]) return;
    state.lang = lang;
    localStorage.setItem("netpulse_lang", lang);
    document.documentElement.lang = lang === "bm" ? "ms" : lang;

    document.querySelectorAll(".lang-btn").forEach((btn) => {
        btn.classList.toggle("active", btn.dataset.lang === lang);
    });
    document.querySelectorAll("[data-i18n]").forEach((el) => {
        const key = el.dataset.i18n;
        if (key) el.textContent = t(key);
    });
    if (state.lastResult) {
        renderResult(state.lastResult.complete, state.lastResult.detected);
    }
}

function setProgress(pct, text) {
    $("progress-wrap").classList.remove("hidden");
    $("progress-fill").style.width = `${Math.max(0, Math.min(100, pct))}%`;
    $("progress-status").textContent = text;
}

function showError(msg) {
    const el = $("error-banner");
    el.textContent = msg;
    el.classList.remove("hidden");
}

function hideError() {
    $("error-banner").classList.add("hidden");
}

function getOptionalGps() {
    return new Promise((resolve) => {
        if (!$("opt-gps").checked || !navigator.geolocation) {
            resolve(null);
            return;
        }
        navigator.geolocation.getCurrentPosition(
            (pos) => resolve({ lat: pos.coords.latitude, lon: pos.coords.longitude }),
            () => resolve(null),
            { enableHighAccuracy: false, timeout: 4000, maximumAge: 60000 }
        );
    });
}

function initTurnstile() {
    if (!TURNSTILE_SITEKEY) return;
    const mount = () => {
        if (!window.turnstile || state.turnstileWidgetId !== null) return;
        state.turnstileWidgetId = window.turnstile.render("#turnstile-container", {
            sitekey: TURNSTILE_SITEKEY,
            theme: "dark",
            callback: (token) => {
                state.turnstileToken = token;
            },
            "expired-callback": () => {
                state.turnstileToken = null;
            },
        });
    };
    if (window.turnstile) {
        mount();
    } else {
        window.addEventListener("load", mount);
    }
}

function badgeClassForVerdict(code) {
    if (code === "NETWORK_HEALTHY") return "badge badge-ok";
    if (code === "DISTRICT_CARRIER_OUTAGE") return "badge badge-err";
    if (code === "VPN_ACTIVE" || code === "LOCAL_PROBE_UNAVAILABLE") return "badge badge-muted";
    return "badge badge-warn";
}

function renderResult(complete, detected) {
    $("result-section").classList.remove("hidden");

    const badge = $("verdict-badge");
    badge.className = badgeClassForVerdict(complete.verdict_code);
    badge.textContent = complete.verdict_code;

    $("result-carrier-district").textContent = `${complete.carrier} · ${complete.district}`;
    $("result-net-meta").textContent = detected
        ? `AS${detected.asn} · ${detected.net_class} · H3 ${detected.h3_res6}`
        : "";

    const verdictText =
        state.lang === "ru"
            ? complete.verdict_text_ru
            : state.lang === "bm"
                ? complete.verdict_text_bm
                : complete.verdict_text_en;
    $("verdict-text").textContent = verdictText;

    const vpnEl = $("vpn-warning");
    if (detected?.exclude_from_heatmap) {
        vpnEl.textContent = t("vpn_warn");
        vpnEl.classList.remove("hidden");
    } else {
        vpnEl.classList.add("hidden");
    }

    const m = complete.metrics;
    $("m-local-rtt").textContent = m.local_rtt_p50_ms != null ? `${m.local_rtt_p50_ms} ms` : "—";
    $("m-intl-rtt").textContent = m.intl_rtt_p50_ms != null ? `${m.intl_rtt_p50_ms} ms` : "—";
    $("m-dl").textContent = m.dl_mbps != null ? `${m.dl_mbps} Mbps` : "—";
    $("m-ul").textContent = m.ul_mbps != null ? `${m.ul_mbps} Mbps` : "—";
    $("m-stall").textContent = m.stall_ratio != null ? `${(m.stall_ratio * 100).toFixed(1)}%` : "—";

    const dc = complete.district_comparison;
    $("m-district").textContent =
        dc && dc.district_health_score != null
            ? `${dc.district_health_score}/100 (${dc.samples_24h})`
            : `— (${dc ? dc.samples_24h : 0})`;
}

async function runDiagnostic() {
    if (state.running) return;
    hideError();

    if (TURNSTILE_SITEKEY && !state.turnstileToken) {
        showError(t("err_turnstile"));
        return;
    }

    state.running = true;
    const btn = $("btn-run-test");
    btn.disabled = true;
    btn.textContent = t("btn_running");

    try {
        setProgress(10, t("step_gps"));
        const clientGeo = await getOptionalGps();

        setProgress(22, t("step_init"));
        const headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": generateIdempotencyKey(),
        };
        if (state.turnstileToken) {
            headers["X-Turnstile-Token"] = state.turnstileToken;
        }

        const effectiveType = navigator.connection?.effectiveType || null;
        const initResp = await fetch(`${API_BASE}/v1/tests/init`, {
            method: "POST",
            headers,
            body: JSON.stringify({
                client_geo: clientGeo,
                connection_hint: effectiveType ? { effective_type: effectiveType } : null,
            }),
        });

        if (initResp.status === 429) {
            const retry = initResp.headers.get("Retry-After") || "60";
            throw new Error(t("err_rate_limit").replace("{s}", retry));
        }
        if (!initResp.ok) {
            const errBody = await initResp.json().catch(() => ({}));
            throw new Error(errBody?.error?.message || `Init failed (${initResp.status})`);
        }

        const initData = await initResp.json();
        setProgress(35, t("step_probe"));

        const phasePct = { connected: 40, ping: 55, download: 72, upload: 84, done: 90 };
        let currentPct = 35;
        const { receipts, failed_nodes } = await runProbePlan(initData.probe_plan, (ev) => {
            const next = phasePct[ev.phase] || currentPct;
            if (next > currentPct) currentPct = next;
            setProgress(currentPct, `${t("step_probe")} [${ev.region}: ${ev.phase}]`);
        });

        setProgress(93, t("step_complete"));
        const completeResp = await fetch(`${API_BASE}/v1/tests/complete`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                test_id: initData.test_id,
                receipts,
                failed_nodes,
            }),
        });

        if (!completeResp.ok) {
            const errBody = await completeResp.json().catch(() => ({}));
            throw new Error(errBody?.error?.message || `Complete failed (${completeResp.status})`);
        }

        const completeData = await completeResp.json();
        state.lastResult = { complete: completeData, detected: initData.detected_network };
        renderResult(completeData, initData.detected_network);
        setProgress(100, "OK");

        await Promise.all([loadIncidents({ fresh: true }), loadHeatmap({ fresh: true, fitToCells: true })]);
    } catch (err) {
        showError(err.message || String(err));
    } finally {
        state.running = false;
        btn.disabled = false;
        btn.textContent = t("btn_start");
        if (window.turnstile && state.turnstileWidgetId !== null) {
            state.turnstileToken = null;
            window.turnstile.reset(state.turnstileWidgetId);
        }
    }
}

async function loadIncidents({ fresh = false } = {}) {
    const container = $("incidents-list");
    try {
        const resp = await fetch(`${API_BASE}/v1/incidents/active`, fresh ? { cache: "no-store" } : undefined);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        const list = data.active_incidents || [];
        state.incidents = list;
        renderMapLayers();

        if (!list.length) {
            container.innerHTML = `<div class="muted small">${escapeHtml(t("no_incidents"))}</div>`;
            return;
        }

        container.innerHTML = list
            .map((inc) => {
                const isMajor = inc.severity === "MAJOR_OUTAGE";
                const badgeCls = isMajor ? "badge badge-err" : "badge badge-warn";
                return `
          <div class="incident-item ${isMajor ? "major" : ""}">
            <div>
              <strong>${escapeHtml(inc.carrier_name)}</strong> — ${escapeHtml(inc.district_name)}
              <div class="muted small">
                ${escapeHtml(inc.unique_reports)} reports · P95 ${escapeHtml(inc.p95_rtt_ms)} ms ·
                Stall ${(inc.avg_stall_ratio * 100).toFixed(1)}%
              </div>
            </div>
            <span class="${badgeCls}">${escapeHtml(inc.severity)}</span>
          </div>
        `;
            })
            .join("");
    } catch {
        container.innerHTML = `<div class="muted small">Unable to load active incidents.</div>`;
    }
}

function updateStatsSummary(cells) {
    $("stat-zones").textContent = String(cells.length);
    const totalUsers = cells.reduce((acc, c) => acc + Number(c.samples || 0), 0);
    $("stat-users").textContent = String(totalUsers);

    if (!cells.length) {
        $("stat-quality").textContent = "—";
        $("stat-quality").style.color = "";
        $("stat-dl").textContent = "—";
        $("stat-rtt").textContent = "—";
        return;
    }

    const avgQuality = Math.round(
        cells.reduce((acc, c) => acc + visualScoreForCell(c), 0) / cells.length
    );
    const dlCells = cells.filter((c) => c.median_dl_kbps != null);
    const avgDl = dlCells.length
        ? (dlCells.reduce((acc, c) => acc + c.median_dl_kbps, 0) / dlCells.length / 1000).toFixed(1)
        : null;
    const avgRtt = (
        cells.reduce((acc, c) => acc + Number(c.p95_rtt_ms || 0), 0) / cells.length
    ).toFixed(0);

    $("stat-quality").textContent = `${avgQuality}/100`;
    $("stat-quality").style.color = scoreToCss(avgQuality, 1);
    $("stat-dl").textContent = avgDl != null ? `${avgDl} Mbps` : "PULSE";
    $("stat-rtt").textContent = `${avgRtt} ms`;
}

async function loadHeatmap({ fresh = false, fitToCells = false } = {}) {
    const carrier = $("filter-carrier").value;
    const win = $("filter-window").value;
    const res = $("filter-res").value;
    const grid = $("heatmap-grid");
    const emptyEl = $("heatmap-empty");
    const meta = $("heatmap-meta");

    const params = new URLSearchParams({ window: win, res });
    if (carrier && carrier !== "all") params.set("carrier", carrier);

    try {
        const resp = await fetch(
            `${API_BASE}/v1/heatmap?${params.toString()}`,
            fresh ? { cache: "no-store" } : undefined
        );
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        const cells = (data.cells || [])
            .slice()
            .sort((a, b) => visualScoreForCell(a) - visualScoreForCell(b));

        state.cells = cells;
        meta.textContent = `${cells.length} zones · min N=${data.min_samples}`;
        updateStatsSummary(cells);
        renderMapLayers({ fitToCells });

        if (!cells.length) {
            emptyEl.textContent = t("no_heatmap").replace("{n}", String(data.min_samples));
            emptyEl.classList.remove("hidden");
            grid.innerHTML = "";
            return;
        }

        emptyEl.classList.add("hidden");
        grid.innerHTML = cells
            .map((c) => {
                const vScore = visualScoreForCell(c);
                const reports = Math.max(1, Number(c.reports || c.samples || 1));
                const dlText = c.median_dl_kbps != null ? `${(c.median_dl_kbps / 1000).toFixed(1)} Mbps` : "PULSE";
                const confCls =
                    c.confidence === "HIGH"
                        ? "badge badge-ok"
                        : c.confidence === "MED"
                            ? "badge badge-warn"
                            : "badge badge-muted";
                const topColor = scoreToCss(vScore, 1);
                return `
          <div class="hex-cell" data-lat="${escapeHtml(c.lat)}" data-lon="${escapeHtml(c.lon)}" style="border-top-color: ${topColor};" title="H3: ${escapeHtml(c.h3)}">
            <div class="hex-top">
              <span class="hex-district">${escapeHtml(c.district)}</span>
              <span class="${confCls}">${escapeHtml(c.confidence)}</span>
            </div>
            <div class="hex-score" style="color: ${topColor};">${vScore}/100</div>
            <div class="hex-meta">
              N=${escapeHtml(c.samples)} (${reports} tests) · P95 ${escapeHtml(c.p95_rtt_ms)} ms<br />
              DL ${escapeHtml(dlText)} · Stall ${(c.avg_stall_ratio * 100).toFixed(1)}%
            </div>
          </div>
        `;
            })
            .join("");

        grid.querySelectorAll(".hex-cell[data-lat][data-lon]").forEach((el) => {
            el.addEventListener("click", () => {
                if (!state.map) return;
                const lat = Number(el.dataset.lat);
                const lon = Number(el.dataset.lon);
                if (Number.isFinite(lat) && Number.isFinite(lon)) {
                    state.map.setView([lat, lon], 14, { animate: true });
                }
            });
        });
    } catch {
        emptyEl.textContent = "Unable to load heatmap data.";
        emptyEl.classList.remove("hidden");
        grid.innerHTML = "";
    }
}

function boot() {
    document.querySelectorAll(".lang-btn").forEach((btn) => {
        btn.addEventListener("click", () => applyLanguage(btn.dataset.lang));
    });
    $("btn-run-test").addEventListener("click", runDiagnostic);
    $("btn-refresh-incidents").addEventListener("click", () => loadIncidents({ fresh: true }));
    $("btn-fit-map").addEventListener("click", fitMapToData);
    $("filter-carrier").addEventListener("change", () => loadHeatmap({ fitToCells: true }));
    $("filter-window").addEventListener("change", () => loadHeatmap({ fitToCells: true }));
    $("filter-res").addEventListener("change", () => loadHeatmap({ fitToCells: true }));

    applyLanguage(state.lang);
    initTurnstile();
    initQualityMap();
    loadIncidents();
    loadHeatmap({ fitToCells: true });

    setInterval(loadIncidents, 60000);
}

boot();
