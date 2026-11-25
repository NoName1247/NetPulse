import os
from datetime import date, timedelta
from typing import Any

import asyncpg
import h3
import structlog

from central.models import (
    AsnRecord,
    CarrierRecord,
    MeasurementRow,
    NetClass,
    NodeConfigRow,
    ReferenceData,
)

logger = structlog.get_logger("central.db")

# Опорные координаты районов Klang Valley и крупных городов Малайзии
# Точные индексы H3 Res 6 вычисляются библиотекой h3 на старте (идемпотентно).
MALAYSIA_DISTRICT_SEEDS: list[tuple[float, float, str, str]] = [
    (3.1579, 101.7116, "KLCC", "W.P. Kuala Lumpur"),
    (3.1478, 101.7135, "Bukit Bintang", "W.P. Kuala Lumpur"),
    (3.0554, 101.7005, "Bukit Jalil", "W.P. Kuala Lumpur"),
    (3.1073, 101.7259, "Cheras", "W.P. Kuala Lumpur"),
    (3.2140, 101.6356, "Kepong", "W.P. Kuala Lumpur"),
    (3.1292, 101.6784, "Bangsar", "W.P. Kuala Lumpur"),
    (3.1704, 101.6524, "Mont Kiara", "W.P. Kuala Lumpur"),
    (3.2046, 101.7196, "Setapak", "W.P. Kuala Lumpur"),
    (3.2038, 101.7367, "Wangsa Maju", "W.P. Kuala Lumpur"),
    (3.0908, 101.6820, "Sri Petaling", "W.P. Kuala Lumpur"),
    (3.1073, 101.6067, "Petaling Jaya", "Selangor"),
    (3.1516, 101.6152, "Damansara", "Selangor"),
    (3.0494, 101.5855, "Subang Jaya", "Selangor"),
    (3.0733, 101.5185, "Shah Alam", "Selangor"),
    (3.0220, 101.6169, "Puchong", "Selangor"),
    (3.1491, 101.7625, "Ampang", "Selangor"),
    (2.9935, 101.7874, "Kajang", "Selangor"),
    (2.9213, 101.6559, "Cyberjaya", "Selangor"),
    (2.9264, 101.6964, "Putrajaya", "W.P. Putrajaya"),
    (3.0449, 101.4456, "Klang", "Selangor"),
    (5.4141, 100.3288, "George Town", "Penang"),
    (5.3991, 100.3638, "Butterworth", "Penang"),
    (1.4927, 103.7414, "Johor Bahru", "Johor"),
    (1.5374, 103.6578, "Skudai", "Johor"),
    (4.5975, 101.0901, "Ipoh", "Perak"),
    (2.1896, 102.2501, "Melaka", "Melaka"),
    (2.7297, 101.9381, "Seremban", "Negeri Sembilan"),
    (3.8077, 103.3260, "Kuantan", "Pahang"),
    (5.9804, 116.0735, "Kota Kinabalu", "Sabah"),
    (1.5533, 110.3592, "Kuching", "Sarawak"),
]

_WINDOW_TO_TIMEDELTA: dict[str, timedelta] = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
}


class Database:
    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 2,
        max_size: int = 10,
        command_timeout_s: float = 5.0,
    ) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._command_timeout_s = command_timeout_s
        self._pool: asyncpg.Pool | None = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database pool is not initialized")
        return self._pool

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(
            dsn=self._dsn,
            min_size=self._min_size,
            max_size=self._max_size,
            command_timeout=self._command_timeout_s,
        )
        logger.info("db_pool_connected", max_size=self._max_size)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            logger.info("db_pool_closed")

    async def ping(self) -> bool:
        try:
            async with self.pool.acquire() as conn:
                val = await conn.fetchval("SELECT 1")
                return val == 1
        except Exception:
            return False

    async def seed_h3_districts(self) -> None:
        rows = [
            (h3.str_to_int(h3.latlng_to_cell(lat, lon, 6)), district, state)
            for lat, lon, district, state in MALAYSIA_DISTRICT_SEEDS
        ]
        async with self.pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO h3_res6_districts (h3_res6, district_name, state_name)
                VALUES ($1, $2, $3)
                ON CONFLICT (h3_res6) DO NOTHING
                """,
                rows,
            )

    async def load_reference_data(self) -> ReferenceData:
        ref = ReferenceData()
        async with self.pool.acquire() as conn:
            carrier_rows = await conn.fetch(
                "SELECT id, slug, display_name, carrier_type FROM carriers ORDER BY id"
            )
            for r in carrier_rows:
                rec = CarrierRecord(
                    id=int(r["id"]),
                    slug=str(r["slug"]),
                    display_name=str(r["display_name"]),
                    carrier_type=int(r["carrier_type"]),
                )
                ref.carriers_by_id[rec.id] = rec
                ref.carriers_by_slug[rec.slug] = rec

            asn_rows = await conn.fetch(
                "SELECT asn, carrier_id, net_class, org_name FROM asn_mapping"
            )
            for r in asn_rows:
                rec_asn = AsnRecord(
                    asn=int(r["asn"]),
                    carrier_id=int(r["carrier_id"]) if r["carrier_id"] is not None else None,
                    net_class=NetClass(int(r["net_class"])),
                    org_name=str(r["org_name"]),
                )
                ref.asn_map[rec_asn.asn] = rec_asn

            node_rows = await conn.fetch(
                """
                SELECT id, region_code, endpoint_url, is_local, is_active
                FROM probe_nodes
                WHERE is_active = TRUE
                ORDER BY id
                """
            )
            for r in node_rows:
                ref.nodes.append(
                    NodeConfigRow(
                        id=int(r["id"]),
                        region_code=str(r["region_code"]),
                        endpoint_url=str(r["endpoint_url"]),
                        is_local=bool(r["is_local"]),
                        is_active=bool(r["is_active"]),
                    )
                )

            district_rows = await conn.fetch(
                "SELECT h3_res6, district_name FROM h3_res6_districts"
            )
            for r in district_rows:
                ref.districts_by_h3[int(r["h3_res6"])] = str(r["district_name"])

        return ref

    async def get_or_create_daily_salt_myt(self) -> tuple[date, bytes]:
        """Получение/создание суточной соли строго по дате Asia/Kuala_Lumpur (MYT, UTC+8)."""
        candidate_salt = os.urandom(32)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO daily_salts (salt_day, salt)
                    VALUES ((NOW() AT TIME ZONE 'Asia/Kuala_Lumpur')::date, $1)
                    ON CONFLICT (salt_day) DO NOTHING
                    """,
                    candidate_salt,
                )
                row = await conn.fetchrow(
                    """
                    SELECT salt_day, salt
                    FROM daily_salts
                    WHERE salt_day = (NOW() AT TIME ZONE 'Asia/Kuala_Lumpur')::date
                    """
                )
                assert row is not None
                return row["salt_day"], bytes(row["salt"])

    async def delete_expired_salts_myt(self) -> int:
        async with self.pool.acquire() as conn:
            status = await conn.execute(
                """
                DELETE FROM daily_salts
                WHERE salt_day < (NOW() AT TIME ZONE 'Asia/Kuala_Lumpur')::date
                """
            )
            return _parse_affected_rows(status)

    async def insert_measurements(self, rows: list[MeasurementRow]) -> None:
        if not rows:
            return
        params = [
            (
                row.test_id,
                row.node_id,
                row.carrier_id,
                row.asn,
                row.net_class,
                row.h3_res8,
                row.h3_res6,
                row.geo_source,
                row.probe_mode,
                row.rtt_p50_ms,
                row.rtt_p95_ms,
                row.jitter_ms,
                row.stall_ratio,
                row.dl_kbps,
                row.ul_kbps,
                row.session_hash,
            )
            for row in rows
        ]
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.executemany(
                    """
                    INSERT INTO probe_measurements (
                        test_id, node_id, carrier_id, asn, net_class,
                        h3_res8, h3_res6, geo_source, probe_mode,
                        rtt_p50_ms, rtt_p95_ms, jitter_ms, stall_ratio,
                        dl_kbps, ul_kbps, session_hash
                    )
                    VALUES (
                        $1, $2, $3, $4, $5,
                        $6, $7, $8, $9,
                        $10, $11, $12, $13,
                        $14, $15, $16
                    )
                    ON CONFLICT (test_id, node_id) DO NOTHING
                    """,
                    params,
                )

    async def fetch_heatmap_rows(
        self,
        *,
        window: str,
        carrier_id: int | None,
        resolution: int,
        min_samples: int = 5,
    ) -> list[dict[str, Any]]:
        interval_td = _WINDOW_TO_TIMEDELTA[window]
        h3_col = "m.h3_res6" if resolution == 6 else "m.h3_res8"
        query = f"""
            SELECT
                {h3_col} AS h3_cell,
                COALESCE(d.district_name, 'Malaysia') AS district_name,
                COUNT(DISTINCT m.session_hash) AS unique_sessions,
                COUNT(*) AS total_reports,
                AVG(m.stall_ratio) AS avg_stall_ratio,
                PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY m.rtt_p95_ms) AS p95_rtt_ms,
                PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY m.dl_kbps) AS median_dl_kbps
            FROM probe_measurements m
            JOIN probe_nodes n ON n.id = m.node_id AND n.is_local = TRUE
            LEFT JOIN h3_res6_districts d ON d.h3_res6 = m.h3_res6
            WHERE m.ts >= NOW() - $1::interval
              AND m.net_class IN (1, 2)
              AND ($2::smallint IS NULL OR m.carrier_id = $2)
            GROUP BY {h3_col}, d.district_name
            HAVING COUNT(DISTINCT m.session_hash) >= $3
        """
        async with self.pool.acquire() as conn:
            records = await conn.fetch(query, interval_td, carrier_id, max(1, int(min_samples)))
            return [dict(r) for r in records]

    async def fetch_district_stats_row(
        self, *, h3_res6: int, carrier_id: int | None
    ) -> dict[str, Any] | None:
        """
        Один проход по индексу (ts DESC, node_id, carrier_id) за 24h:
        - окно 1h используется для Rule Engine и district_health_score;
        - samples_24h честно отражает число уникальных сессий за 24h.
        """
        query = """
            SELECT
                COUNT(DISTINCT m.session_hash) FILTER (
                    WHERE m.ts >= NOW() - INTERVAL '1 hour'
                ) AS unique_sessions_1h,
                AVG(m.stall_ratio) FILTER (
                    WHERE m.ts >= NOW() - INTERVAL '1 hour'
                ) AS avg_stall_ratio_1h,
                PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY m.rtt_p95_ms) FILTER (
                    WHERE m.ts >= NOW() - INTERVAL '1 hour'
                ) AS p95_rtt_ms_1h,
                PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY m.dl_kbps) FILTER (
                    WHERE m.ts >= NOW() - INTERVAL '1 hour'
                ) AS median_dl_kbps_1h,
                COUNT(DISTINCT m.session_hash) AS samples_24h
            FROM probe_measurements m
            JOIN probe_nodes n ON n.id = m.node_id AND n.is_local = TRUE
            WHERE m.ts >= NOW() - INTERVAL '24 hours'
              AND m.net_class IN (1, 2)
              AND m.h3_res6 = $1
              AND ($2::smallint IS NULL OR m.carrier_id = $2)
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, h3_res6, carrier_id)
            return dict(row) if row is not None else None

    async def fetch_active_incidents_rows(self) -> list[dict[str, Any]]:
        query = """
            SELECT
                i.id,
                c.slug AS carrier_slug,
                c.display_name AS carrier_name,
                COALESCE(d.district_name, 'Malaysia') AS district_name,
                i.h3_res6,
                i.severity,
                i.verdict_code,
                i.unique_reports,
                i.avg_stall_bps,
                i.p95_rtt_ms,
                i.started_at,
                i.last_seen_at
            FROM outage_incidents i
            JOIN carriers c ON c.id = i.carrier_id
            LEFT JOIN h3_res6_districts d ON d.h3_res6 = i.h3_res6
            WHERE i.resolved_at IS NULL
            ORDER BY i.last_seen_at DESC
            LIMIT 100
        """
        async with self.pool.acquire() as conn:
            records = await conn.fetch(query)
            return [dict(r) for r in records]

    async def run_outage_detection_cycle(self) -> tuple[int, int]:
        """Шаг A (UPSERT активных сбоев N>=15 за 15m) + Шаг B (автозакрытие после 20m тишины)."""
        upsert_sql = """
            WITH current_bad AS (
                SELECT
                    m.carrier_id,
                    m.h3_res6,
                    COUNT(DISTINCT m.session_hash) AS uniq_users,
                    ROUND(AVG(m.stall_ratio) * 10000)::int AS avg_stall_bps,
                    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY m.rtt_p95_ms) AS cur_p95
                FROM probe_measurements m
                JOIN probe_nodes n ON n.id = m.node_id AND n.is_local = TRUE
                WHERE m.ts >= NOW() - INTERVAL '15 minutes'
                  AND m.net_class IN (1, 2)
                  AND m.carrier_id IS NOT NULL
                GROUP BY m.carrier_id, m.h3_res6
                HAVING COUNT(DISTINCT m.session_hash) >= 15
                   AND (
                       AVG(m.stall_ratio) >= 0.15
                       OR PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY m.rtt_p95_ms) > 220
                       OR PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY m.dl_kbps) < 1500
                   )
            )
            INSERT INTO outage_incidents (
                carrier_id, h3_res6, severity, verdict_code,
                avg_stall_bps, p95_rtt_ms, unique_reports
            )
            SELECT
                carrier_id,
                h3_res6,
                CASE
                    WHEN avg_stall_bps >= 2500 OR uniq_users >= 30 THEN 'MAJOR_OUTAGE'
                    ELSE 'DEGRADED'
                END,
                'DISTRICT_CARRIER_OUTAGE',
                avg_stall_bps,
                cur_p95,
                uniq_users
            FROM current_bad
            ON CONFLICT (carrier_id, h3_res6) WHERE resolved_at IS NULL
            DO UPDATE SET
                severity       = EXCLUDED.severity,
                avg_stall_bps  = EXCLUDED.avg_stall_bps,
                p95_rtt_ms     = EXCLUDED.p95_rtt_ms,
                unique_reports = EXCLUDED.unique_reports,
                last_seen_at   = NOW()
        """
        resolve_sql = """
            UPDATE outage_incidents
            SET resolved_at = NOW()
            WHERE resolved_at IS NULL
              AND last_seen_at < NOW() - INTERVAL '20 minutes'
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                upserted_status = await conn.execute(upsert_sql)
                resolved_status = await conn.execute(resolve_sql)
        return _parse_affected_rows(upserted_status), _parse_affected_rows(resolved_status)

    async def delete_old_measurements_batch(
        self, *, retention_days: int = 180, limit: int = 50000
    ) -> int:
        cutoff_interval = timedelta(days=retention_days)
        async with self.pool.acquire() as conn:
            status = await conn.execute(
                """
                DELETE FROM probe_measurements
                WHERE ctid IN (
                    SELECT ctid FROM probe_measurements
                    WHERE ts < NOW() - $1::interval
                    LIMIT $2
                )
                """,
                cutoff_interval,
                limit,
            )
            return _parse_affected_rows(status)


def _parse_affected_rows(status: str) -> int:
    try:
        return int(status.split()[-1])
    except (IndexError, ValueError):
        return 0