import h3

from central.db import Database
from central.models import (
    ActiveIncidentItem,
    ActiveIncidentsResponse,
    BadRequestError,
    ConfidenceLevel,
    DistrictStats,
    FailedNodeIn,
    HealthStatus,
    HeatmapCell,
    HeatmapResolution,
    HeatmapResponse,
    HeatmapWindow,
    NetClass,
    ReceiptIn,
    VerdictCode,
    VerdictResult,
)
from central.state import InMemoryState

MIN_HEATMAP_SAMPLES = 5
OUTAGE_SAMPLE_THRESHOLD = 15


def compute_raw_score(
    avg_stall_ratio: float,
    p95_rtt_ms: float,
    median_dl_kbps: float | None,
) -> float:
    """
    Формула Raw Score (0..100) из Модуля 5.2:
    100 - min(50, 250*stall) - min(30, max(0, (p95-50)/6)) - min(20, max(0, (10000-COALESCE(dl, 5000))/500))
    """
    effective_dl = 5000.0 if median_dl_kbps is None else float(median_dl_kbps)
    stall_penalty = min(50.0, 250.0 * max(0.0, float(avg_stall_ratio)))
    rtt_penalty = min(30.0, max(0.0, (float(p95_rtt_ms) - 50.0) / 6.0))
    dl_penalty = min(20.0, max(0.0, (10000.0 - effective_dl) / 500.0))
    return max(0.0, min(100.0, 100.0 - stall_penalty - rtt_penalty - dl_penalty))


def compute_health_score(
    raw_score: float,
    n_samples: int,
    *,
    prior_mean: float = 75.0,
    prior_weight: float = 15.0,
) -> int:
    """Байесовское сглаживание Health Score (C=75, m=15) из Модуля 5.2."""
    n = max(0, int(n_samples))
    smoothed = (n * raw_score + prior_weight * prior_mean) / (n + prior_weight)
    return max(0, min(100, round(smoothed)))


def confidence_for_samples(n_samples: int) -> ConfidenceLevel:
    if n_samples >= 50:
        return "HIGH"
    if n_samples >= 15:
        return "MED"
    return "LOW"


def status_for_health_score(health_score: int) -> HealthStatus:
    if health_score >= 75:
        return "HEALTHY"
    if health_score >= 50:
        return "DEGRADED"
    return "MAJOR_OUTAGE"


def is_local_receipt_degraded(receipt: ReceiptIn) -> tuple[bool, bool]:
    """
    Возвращает (my_degraded, only_throttling) строго по формуле Модуля 5.1:
    my_degraded = stall_ratio > 0.15 or rtt_p95_ms > 150.0
                  or (probe_mode == FULL_PROBE and dl_kbps is not None and dl_kbps < 1500)
    """
    m = receipt.metrics
    ping_or_stall_bad = m.stall_ratio > 0.15 or m.rtt_p95_ms > 150.0
    throttling_bad = (
        receipt.probe_mode == "FULL_PROBE"
        and m.dl_kbps is not None
        and m.dl_kbps < 1500
    )
    my_degraded = ping_or_stall_bad or throttling_bad
    only_throttling = throttling_bad and not ping_or_stall_bad
    return my_degraded, only_throttling


def evaluate_verdict(
    *,
    my_receipt: ReceiptIn | None,
    eu_receipt: ReceiptIn | None,
    failed_nodes: list[FailedNodeIn],
    local_node_ids: set[int],
    district_stats: DistrictStats | None,
    net_class: NetClass,
    carrier_name: str,
    district_name: str,
    local_probe_unavailable: bool = False,
) -> VerdictResult:
    """Детерминированный Rule Engine (RU / EN / BM, 0 LLM, < 2ms)."""
    # 1. VPN / Datacenter — высший приоритет
    if net_class == NetClass.VPN_DATACENTER:
        return VerdictResult(
            verdict_code=VerdictCode.VPN_ACTIVE,
            verdict_text_ru=(
                "Обнаружен VPN/Proxy. Замер выполнен, но реальный оператор не определён "
                "и данные не учтены в карте покрытия."
            ),
            verdict_text_en=(
                "VPN/Proxy connection detected. Test completed, but carrier is unidentified "
                "and data is excluded from public coverage maps."
            ),
            verdict_text_bm=(
                "Sambungan VPN/Proksi dikesan. Ujian selesai, tetapi pembekal perkhidmatan "
                "sebenar tidak dikenal pasti dan data dikecualikan daripada peta liputan."
            ),
        )

    failed_local = any(fn.node_id in local_node_ids for fn in failed_nodes)
    failed_intl = any(fn.node_id not in local_node_ids for fn in failed_nodes)
    intl_degraded = failed_intl or (
        eu_receipt is not None and eu_receipt.metrics.stall_ratio > 0.15
    )

    # 2. Деградация флота MVP: локальная нода MY-KUL была недоступна ещё на этапе init
    if local_probe_unavailable and my_receipt is None:
        if intl_degraded:
            return VerdictResult(
                verdict_code=VerdictCode.INTL_ROUTE_DEGRADED,
                verdict_text_ru=(
                    "Локальная нода замера в Малайзии временно недоступна, а международный "
                    "маршрут до Европы показывает деградацию — возможны проблемы на зарубежном шлюзе."
                ),
                verdict_text_en=(
                    "Local probe node in Malaysia is temporarily unavailable, and international "
                    "routing to Europe is degraded — likely an overseas gateway issue."
                ),
                verdict_text_bm=(
                    "Nod ujian tempatan di Malaysia tidak tersedia buat sementara waktu, dan laluan "
                    "antarabangsa ke Eropah terjejas — kemungkinan masalah pintu masuk antarabangsa."
                ),
            )
        eu_rtt = f"{eu_receipt.metrics.rtt_p50_ms:.1f}" if eu_receipt is not None else "N/A"
        return VerdictResult(
            verdict_code=VerdictCode.LOCAL_PROBE_UNAVAILABLE,
            verdict_text_ru=(
                f"Локальная нода замера в Малайзии временно недоступна; международный канал "
                f"до Европы работает стабильно ({eu_rtt} ms)."
            ),
            verdict_text_en=(
                f"Local Malaysia probe node is temporarily unavailable; international route "
                f"to Europe is stable ({eu_rtt} ms)."
            ),
            verdict_text_bm=(
                f"Nod ujian tempatan di Malaysia tidak tersedia buat sementara waktu; laluan "
                f"antarabangsa ke Eropah stabil ({eu_rtt} ms)."
            ),
        )

    # 3. Оценка локального замера MY (если MY-нода была в плане, но упала у клиента — это деградация)
    if my_receipt is not None:
        my_degraded, only_throttling = is_local_receipt_degraded(my_receipt)
        my_rtt_str = f"{my_receipt.metrics.rtt_p50_ms:.1f}"
        dl_mbps_val = (
            my_receipt.metrics.dl_kbps / 1000.0
            if my_receipt.metrics.dl_kbps is not None
            else None
        )
        my_dl_str = f"{dl_mbps_val:.2f}" if dl_mbps_val is not None else "N/A"
    else:
        my_degraded = True if failed_local else True
        only_throttling = False
        my_rtt_str = "timeout"
        my_dl_str = "0.00"

    # 4. MY OK, но зарубежный маршрут EU деградировал / timeout
    if not my_degraded and intl_degraded:
        return VerdictResult(
            verdict_code=VerdictCode.INTL_ROUTE_DEGRADED,
            verdict_text_ru=(
                f"Локальная сеть в Малайзии работает стабильно ({my_rtt_str} ms), но фиксируется "
                "деградация международного маршрута до Европы — возможны проблемы на зарубежном "
                "шлюзе/подводном кабеле."
            ),
            verdict_text_en=(
                f"Local network in Malaysia is stable ({my_rtt_str} ms), but international "
                "routing to Europe is degraded — likely an overseas gateway or submarine cable issue."
            ),
            verdict_text_bm=(
                f"Rangkaian tempatan di Malaysia beroperasi dengan stabil ({my_rtt_str} ms), "
                "tetapi laluan antarabangsa ke Eropah terjejas — kemungkinan masalah pintu masuk "
                "antarabangsa atau kabel dasar laut."
            ),
        )

    # 5. Локальный маршрут MY деградировал -> сверяем с консенсусом района (H3 Res 6)
    if my_degraded:
        n_recent = district_stats.unique_sessions_recent if district_stats else 0
        district_health = district_stats.health_score if district_stats else None

        if n_recent >= OUTAGE_SAMPLE_THRESHOLD and district_health is not None and district_health < 75:
            if district_health < 50:
                return VerdictResult(
                    verdict_code=VerdictCode.DISTRICT_CARRIER_OUTAGE,
                    verdict_text_ru=(
                        f"Подтверждён массовый сбой {carrier_name} в районе {district_name} "
                        f"(подтверждено {n_recent} пользователями за последний час). "
                        "Это проблема на стороне оператора, не вашего устройства."
                    ),
                    verdict_text_en=(
                        f"Mass outage confirmed for {carrier_name} in {district_name} "
                        f"({n_recent} users confirmed in the past hour). "
                        "This is an operator-side issue, not your device."
                    ),
                    verdict_text_bm=(
                        f"Gangguan besar disahkan untuk {carrier_name} di {district_name} "
                        f"(disahkan oleh {n_recent} pengguna dalam sejam yang lalu). "
                        "Ini adalah masalah pihak syarikat telekomunikasi, bukan peranti anda."
                    ),
                )
            # 50 <= district_health < 75 при N >= 15: подтверждённая частичная деградация оператора в районе
            return VerdictResult(
                verdict_code=VerdictCode.DISTRICT_CARRIER_OUTAGE,
                verdict_text_ru=(
                    f"В районе {district_name} фиксируется деградация сети {carrier_name} "
                    f"(данные {n_recent} пользователей за последний час, индекс {district_health}/100). "
                    "Проблема наблюдается на стороне оператора."
                ),
                verdict_text_en=(
                    f"Network degradation detected for {carrier_name} in {district_name} "
                    f"({n_recent} users in the past hour, health score {district_health}/100). "
                    "This appears to be an operator-side issue."
                ),
                verdict_text_bm=(
                    f"Kemerosotan rangkaian dikesan untuk {carrier_name} di {district_name} "
                    f"({n_recent} pengguna dalam sejam yang lalu, skor {district_health}/100). "
                    "Masalah ini berpunca daripada pihak syarikat telekomunikasi."
                ),
            )

        if n_recent >= OUTAGE_SAMPLE_THRESHOLD and district_health is not None and district_health >= 75:
            if only_throttling:
                return VerdictResult(
                    verdict_code=VerdictCode.LOCAL_DEVICE_OR_INDOOR,
                    verdict_text_ru=(
                        f"Зафиксировано сильное ограничение скорости ({my_dl_str} Mbps) при стабильном "
                        "пинге — вероятна перегрузка базовой станции или шейпинг тарифа."
                    ),
                    verdict_text_en=(
                        f"Severe speed throttling detected ({my_dl_str} Mbps) with stable latency — "
                        f"other {carrier_name} users in {district_name} have stable connection. "
                        "Likely cell tower congestion or plan throttling."
                    ),
                    verdict_text_bm=(
                        f"Sekatan kelajuan ketara dikesan ({my_dl_str} Mbps) dengan ping stabil — "
                        f"pengguna {carrier_name} lain di {district_name} mempunyai sambungan stabil. "
                        "Kemungkinan kesesakan menara atau had kelajuan pelan."
                    ),
                )
            return VerdictResult(
                verdict_code=VerdictCode.LOCAL_DEVICE_OR_INDOOR,
                verdict_text_ru=(
                    f"У других пользователей {carrier_name} в {district_name} связь стабильна. "
                    "Проблема, вероятно, локальная (слабый сигнал в здании, перегруженный "
                    "Wi-Fi-роутер или ограничения тарифа)."
                ),
                verdict_text_en=(
                    f"Other {carrier_name} users in {district_name} have stable connection. "
                    "Issue is likely local (weak indoor signal, congested Wi-Fi router, "
                    "or account throttled)."
                ),
                verdict_text_bm=(
                    f"Pengguna {carrier_name} lain di {district_name} mempunyai sambungan yang stabil. "
                    "Masalah mungkin setempat (isyarat dalam bangunan lemah, Wi-Fi sesak, "
                    "atau had pelan)."
                ),
            )

        return VerdictResult(
            verdict_code=VerdictCode.DEGRADED_LOW_SAMPLE,
            verdict_text_ru=(
                f"Зафиксировано ухудшение связи ({my_rtt_str} ms, {my_dl_str} Mbps), "
                f"но по району {district_name} пока недостаточно замеров для подтверждения "
                f"массовой аварии ({n_recent}/{OUTAGE_SAMPLE_THRESHOLD})."
            ),
            verdict_text_en=(
                f"Connection degradation detected ({my_rtt_str} ms, {my_dl_str} Mbps), "
                f"but insufficient samples in {district_name} to confirm mass outage "
                f"({n_recent}/{OUTAGE_SAMPLE_THRESHOLD})."
            ),
            verdict_text_bm=(
                f"Kemerosotan sambungan dikesan ({my_rtt_str} ms, {my_dl_str} Mbps), "
                f"tetapi sampel tidak mencukupi di {district_name} untuk mengesahkan "
                f"gangguan besar ({n_recent}/{OUTAGE_SAMPLE_THRESHOLD})."
            ),
        )

    # 6. MY OK и EU OK -> сеть стабильна
    return VerdictResult(
        verdict_code=VerdictCode.NETWORK_HEALTHY,
        verdict_text_ru=(
            f"Соединение {carrier_name} в районе {district_name} стабильно "
            "по локальным и международным маршрутам."
        ),
        verdict_text_en=(
            f"Connection {carrier_name} in {district_name} is stable "
            "on both local and international routes."
        ),
        verdict_text_bm=(
            f"Sambungan {carrier_name} di {district_name} adalah stabil "
            "pada kedua-dua laluan tempatan dan antarabangsa."
        ),
    )


class AnalyticsService:
    """Аналитика тепловых карт, районных статистик и активных инцидентов."""

    def __init__(self, db: Database, state: InMemoryState, *, min_samples: int = MIN_HEATMAP_SAMPLES) -> None:
        self._db = db
        self._state = state
        self._min_samples = max(1, int(min_samples))

    async def get_district_stats(
        self, *, h3_res6: int, carrier_id: int | None
    ) -> DistrictStats:
        row = await self._db.fetch_district_stats_row(h3_res6=h3_res6, carrier_id=carrier_id)
        if not row:
            return DistrictStats(
                unique_sessions_recent=0,
                avg_stall_ratio=None,
                p95_rtt_ms=None,
                median_dl_kbps=None,
                health_score=None,
                status=None,
                samples_24h=0,
            )

        unique_1h = int(row.get("unique_sessions_1h") or 0)
        samples_24h = int(row.get("samples_24h") or 0)
        avg_stall = (
            float(row["avg_stall_ratio_1h"])
            if row.get("avg_stall_ratio_1h") is not None
            else None
        )
        p95_rtt = (
            float(row["p95_rtt_ms_1h"])
            if row.get("p95_rtt_ms_1h") is not None
            else None
        )
        median_dl = (
            float(row["median_dl_kbps_1h"])
            if row.get("median_dl_kbps_1h") is not None
            else None
        )

        health_score: int | None = None
        status: HealthStatus | None = None
        if unique_1h > 0 and avg_stall is not None and p95_rtt is not None:
            raw = compute_raw_score(avg_stall, p95_rtt, median_dl)
            health_score = compute_health_score(raw, unique_1h)
            status = status_for_health_score(health_score)

        return DistrictStats(
            unique_sessions_recent=unique_1h,
            avg_stall_ratio=avg_stall,
            p95_rtt_ms=p95_rtt,
            median_dl_kbps=median_dl,
            health_score=health_score,
            status=status,
            samples_24h=samples_24h,
        )

    async def get_heatmap(
        self,
        *,
        carrier_slug: str | None,
        window: HeatmapWindow,
        resolution: HeatmapResolution,
    ) -> HeatmapResponse:
        normalized = (carrier_slug or "all").strip().lower()
        carrier_id: int | None = None
        if normalized != "all":
            carrier_rec = self._state.ref_data.carriers_by_slug.get(normalized)
            if carrier_rec is None:
                raise BadRequestError(f"Unknown carrier slug: {carrier_slug}")
            carrier_id = carrier_rec.id

        cache_key = (normalized, window, int(resolution))
        cached = self._state.heatmap_cache.get(cache_key)
        if cached is not None:
            return cached

        rows = await self._db.fetch_heatmap_rows(
            window=window,
            carrier_id=carrier_id,
            resolution=int(resolution),
            min_samples=self._min_samples,
        )
        cells: list[HeatmapCell] = []
        for row in rows:
            samples = int(row["unique_sessions"])
            reports = int(row.get("total_reports") or samples)
            avg_stall = round(float(row["avg_stall_ratio"]), 4)
            p95_rtt = round(float(row["p95_rtt_ms"]), 1)
            median_dl_raw = row["median_dl_kbps"]
            median_dl = int(round(float(median_dl_raw))) if median_dl_raw is not None else None

            h3_hex = h3.int_to_str(int(row["h3_cell"]))
            lat, lon = h3.cell_to_latlng(h3_hex)
            raw_score = compute_raw_score(avg_stall, p95_rtt, median_dl)
            health_score = compute_health_score(raw_score, samples)
            cells.append(
                HeatmapCell(
                    h3=h3_hex,
                    district=str(row["district_name"]),
                    lat=round(lat, 6),
                    lon=round(lon, 6),
                    samples=samples,
                    reports=reports,
                    confidence=confidence_for_samples(samples),
                    avg_stall_ratio=avg_stall,
                    p95_rtt_ms=p95_rtt,
                    median_dl_kbps=median_dl,
                    health_score=health_score,
                    raw_score=max(0, min(100, round(raw_score))),
                    status=status_for_health_score(health_score),
                )
            )

        response = HeatmapResponse(
            window=window,
            carrier=normalized,
            resolution=resolution,
            min_samples=self._min_samples,
            cells=cells,
        )
        self._state.heatmap_cache.set(cache_key, response)
        return response

    async def get_active_incidents(self) -> ActiveIncidentsResponse:
        rows = await self._db.fetch_active_incidents_rows()
        items = [
            ActiveIncidentItem(
                id=int(r["id"]),
                carrier_slug=str(r["carrier_slug"]),
                carrier_name=str(r["carrier_name"]),
                district_name=str(r["district_name"]),
                h3_res6=h3.int_to_str(int(r["h3_res6"])),
                severity=r["severity"],
                verdict_code=str(r["verdict_code"]),
                unique_reports=int(r["unique_reports"]),
                avg_stall_ratio=round(int(r["avg_stall_bps"]) / 10000.0, 4),
                p95_rtt_ms=round(float(r["p95_rtt_ms"]), 1),
                started_at=r["started_at"],
                last_seen_at=r["last_seen_at"],
            )
            for r in rows
        ]
        return ActiveIncidentsResponse(active_incidents=items)