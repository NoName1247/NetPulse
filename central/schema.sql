-- Vanilla PostgreSQL 16 DDL (Lean MVP: без партиций, без Materialized Views, без PostGIS)

-- 1. Суточная соль (таймзона строго Asia/Kuala_Lumpur!)
CREATE TABLE IF NOT EXISTS daily_salts (
    salt_day DATE PRIMARY KEY,
    salt     BYTEA NOT NULL
);

-- 2. Справочник операторов и ASN
CREATE TABLE IF NOT EXISTS carriers (
    id           SMALLSERIAL PRIMARY KEY,
    slug         VARCHAR(32) UNIQUE NOT NULL,
    display_name VARCHAR(64) NOT NULL,
    carrier_type SMALLINT NOT NULL -- 1=Mobile, 2=Fixed
);

CREATE TABLE IF NOT EXISTS asn_mapping (
    asn        INT PRIMARY KEY,
    carrier_id SMALLINT REFERENCES carriers(id),
    net_class  SMALLINT NOT NULL, -- 1=Mobile, 2=Fixed, 3=VPN/DC, 4=Unknown
    org_name   VARCHAR(120) NOT NULL
);

-- 3. Реестр Probe-нод с явным флагом is_local (MY vs EU не смешиваются!)
CREATE TABLE IF NOT EXISTS probe_nodes (
    id           SMALLINT PRIMARY KEY,
    region_code  VARCHAR(16) UNIQUE NOT NULL, -- 'MY-KUL', 'EU-FRA', 'SG-SIN'
    endpoint_url VARCHAR(128) NOT NULL,
    is_local     BOOLEAN NOT NULL DEFAULT FALSE,
    is_active    BOOLEAN NOT NULL DEFAULT TRUE
);

-- 4. Справочник районов (используется через LEFT JOIN, без жёсткого FK)
CREATE TABLE IF NOT EXISTS h3_res6_districts (
    h3_res6       BIGINT PRIMARY KEY,
    district_name VARCHAR(80) NOT NULL,
    state_name    VARCHAR(60) NOT NULL
);

-- 5. Единая таблица замеров (без партиций + честный PK (test_id, node_id))
CREATE TABLE IF NOT EXISTS probe_measurements (
    test_id      UUID        NOT NULL,
    node_id      SMALLINT    NOT NULL REFERENCES probe_nodes(id),
    ts           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    carrier_id   SMALLINT    REFERENCES carriers(id),
    asn          INT         NOT NULL,
    net_class    SMALLINT    NOT NULL, -- 1=Mobile, 2=Fixed, 3=VPN, 4=Unknown
    h3_res8      BIGINT      NOT NULL,
    h3_res6      BIGINT      NOT NULL,
    geo_source   SMALLINT    NOT NULL, -- 1=BrowserGPS, 2=GeoIP
    probe_mode   SMALLINT    NOT NULL, -- 1=FULL_PROBE, 2=PULSE_ONLY
    rtt_p50_ms   REAL        NOT NULL,
    rtt_p95_ms   REAL        NOT NULL,
    jitter_ms    REAL        NOT NULL,
    stall_ratio  REAL        NOT NULL,
    dl_kbps      INT,                  -- NULL для PULSE_ONLY (PERCENTILE_CONT игнорирует NULL!)
    ul_kbps      INT,                  -- NULL для PULSE_ONLY
    session_hash BYTEA       NOT NULL,
    PRIMARY KEY (test_id, node_id)
);

-- Главный индекс: ускоряет выборки за 15 мин и 24 ч до < 5 ms!
CREATE INDEX IF NOT EXISTS idx_pm_ts_valid
    ON probe_measurements (ts DESC, node_id, carrier_id)
    WHERE net_class IN (1, 2);

-- 6. Инциденты (без жёсткого FK на h3_res6 -> не падает в Пенанге/Джохоре!)
CREATE TABLE IF NOT EXISTS outage_incidents (
    id             BIGSERIAL PRIMARY KEY,
    carrier_id     SMALLINT    NOT NULL REFERENCES carriers(id),
    h3_res6        BIGINT      NOT NULL,
    severity       VARCHAR(16) NOT NULL, -- DEGRADED | MAJOR_OUTAGE
    verdict_code   VARCHAR(32) NOT NULL,
    avg_stall_bps  INT         NOT NULL,
    p95_rtt_ms     REAL        NOT NULL,
    unique_reports INT         NOT NULL,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at    TIMESTAMPTZ -- NULL = активный, NOT NULL = автозакрыт
);

-- Уникальность открытого инцидента по (carrier_id, h3_res6) для атомарного UPSERT
CREATE UNIQUE INDEX IF NOT EXISTS idx_outage_active_uniq
    ON outage_incidents (carrier_id, h3_res6)
    WHERE resolved_at IS NULL;