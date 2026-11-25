-- Начальные справочники Малайзии (идемпотентная загрузка)
-- Примечание: полная сетка районов Klang Valley и штатов Малайзии для h3_res6_districts
-- дополнительно точно вычисляется через h3.latlng_to_cell(lat, lon, 6) при старте приложения (central/db.py).

-- 1. 6 основных телеком-операторов Малайзии (carrier_type: 1=Mobile, 2=Fixed)
INSERT INTO carriers (id, slug, display_name, carrier_type) VALUES
    (1, 'maxis',      'Maxis / Hotlink',           1),
    (2, 'celcomdigi', 'CelcomDigi',                1),
    (3, 'umobile',    'U Mobile',                  1),
    (4, 'unifi',      'Unifi (Telekom Malaysia)',  2),
    (5, 'time',       'TIME dotCom',               2),
    (6, 'yes',        'Yes 5G (YTL)',              1)
ON CONFLICT (slug) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    carrier_type = EXCLUDED.carrier_type;

SELECT setval(pg_get_serial_sequence('carriers', 'id'), COALESCE((SELECT MAX(id) FROM carriers), 1));

-- 2. Маппинг ASN Малайзии (APNIC/MCMC) и известных VPN/Cloud/Datacenter ASN
-- net_class: 1=MOBILE_CARRIER, 2=FIXED_BROADBAND, 3=VPN_DATACENTER, 4=UNKNOWN
INSERT INTO asn_mapping (asn, carrier_id, net_class, org_name) VALUES
    -- Maxis / Hotlink
    (9534,   1, 1, 'Maxis Broadband Sdn Bhd'),
    (132434, 1, 1, 'Maxis Broadband Sdn Bhd'),
    -- CelcomDigi (Celcom + DiGi)
    (10030,  2, 1, 'Celcom Axiata Berhad'),
    (4818,   2, 1, 'DiGi Telecommunications Sdn. Bhd.'),
    (10081,  2, 1, 'DiGi Telecommunications Sdn. Bhd.'),
    -- U Mobile
    (38466,  3, 1, 'U Mobile Sdn Bhd'),
    -- Unifi / Telekom Malaysia (Fixed + Unifi Mobile / Webe)
    (4788,   4, 2, 'TM Technology Services Sdn. Bhd. (Unifi)'),
    (17971,  4, 2, 'TM Technology Services Sdn. Bhd.'),
    (18206,  4, 2, 'TM Technology Services Sdn. Bhd.'),
    (38322,  4, 1, 'Webe Digital / Unifi Mobile'),
    -- TIME dotCom
    (9930,   5, 2, 'TIME dotCom Berhad'),
    -- Yes 5G (YTL Communications)
    (45960,  6, 1, 'YTL Communications Sdn Bhd (Yes 5G)'),
    -- VPN / Private Relay / Cloud / Hosting Datacenters (жёстко исключаются из карт операторов)
    (13335,  NULL, 3, 'Cloudflare / WARP / iCloud Private Relay'),
    (16509,  NULL, 3, 'Amazon Web Services'),
    (14618,  NULL, 3, 'Amazon.com, Inc.'),
    (15169,  NULL, 3, 'Google LLC'),
    (396982, NULL, 3, 'Google Cloud'),
    (8075,   NULL, 3, 'Microsoft Corporation'),
    (14061,  NULL, 3, 'DigitalOcean, LLC'),
    (24940,  NULL, 3, 'Hetzner Online GmbH'),
    (20473,  NULL, 3, 'The Constant Company (Vultr)'),
    (16276,  NULL, 3, 'OVH SAS'),
    (9009,   NULL, 3, 'M247 Europe SRL (VPN)'),
    (212238, NULL, 3, 'Datacamp Limited (VPN)'),
    (60068,  NULL, 3, 'Datacamp Limited (VPN)'),
    (55720,  NULL, 3, 'Gigabit Hosting Sdn Bhd'),
    (45352,  NULL, 3, 'IP ServerOne Solutions Sdn Bhd'),
    (149440, NULL, 3, 'Evoxt Sdn. Bhd.'),
    (45839,  NULL, 3, 'Shinjiru Technology Sdn Bhd')
ON CONFLICT (asn) DO UPDATE SET
    carrier_id = EXCLUDED.carrier_id,
    net_class  = EXCLUDED.net_class,
    org_name   = EXCLUDED.org_name;

-- 3. Реестр Probe-нод MVP (MY-KUL локальная, EU-FRA зарубежная)
INSERT INTO probe_nodes (id, region_code, endpoint_url, is_local, is_active) VALUES
    (1, 'MY-KUL', 'https://my-node.netpulse.my', TRUE,  TRUE),
    (2, 'EU-FRA', 'https://eu-node.netpulse.my', FALSE, TRUE)
ON CONFLICT (id) DO UPDATE SET
    region_code  = EXCLUDED.region_code,
    endpoint_url = EXCLUDED.endpoint_url,
    is_local     = EXCLUDED.is_local,
    is_active    = EXCLUDED.is_active;

-- 4. Базовая запись из спецификации (остальные районы КЛ и Малайзии сидятся через h3 в central/db.py)
INSERT INTO h3_res6_districts (h3_res6, district_name, state_name) VALUES
    (X'0866506667ffffff'::bigint, 'Bukit Jalil', 'W.P. Kuala Lumpur')
ON CONFLICT (h3_res6) DO NOTHING;