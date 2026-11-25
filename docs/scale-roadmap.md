# NetPulse MY — Phase 2 & Scale Roadmap

Документ фиксирует запланированные шаги эволюции после запуска Lean MVP (3 контейнера, 2 ноды MY + EU, 0 Valkey, 0 AI).

## 1. Чистый UDP Packet Loss % (Rust Sidecar на Probe-нодах)
* **Почему отложено с Day-1:** Использование `aiortc` на Python тянет нативные кодеки/FFmpeg и создаёт риск утечек памяти/OOM на 1 ГБ VPS, а также требует открытия широких диапазонов UDP-портов и STUN/TURN.
* **Реализация Phase 2:**
  * Сверхлёгкий нативный бинарник на Rust (`webrtc-rs` DataChannel в unreliable/unordered режиме `maxRetransmits: 0`, без медиа-кодеков и FFmpeg).
  * Запускается сайдкаром рядом с `probe-daemon` на нодах; принимает тот же подписанный `probe_token` и добавляет поле `udp_loss_ratio` в канонический HMAC-чек `shared_proto`.

## 2. Расширение флота Probe-нод (`SG-SIN` и `JP-TYO`, Failover `MY-KUL`)
* **Триггер:** Рост аудитории в Восточной Малайзии / Джохоре или требование SLA 99.95% на локальный замер.
* **Реализация:**
  * Добавление `SG-SIN` (~8–12 мс от КЛ) в таблицу `probe_nodes` с резервным флагом failover: при падении `MY-KUL` (3 пропуска `/healthz`) `DiagnosticsService` прозрачно переключает локальный трафик на `SG-SIN` с пометкой `fallback_node=true` в ответе.
  * Добавление `JP-TYO` для разделения азиатского и европейского международных маршрутов (диагностика обрывов подводных кабелей в сторону Тихого океана vs Индийского океана).
  * Расчётный OPEX при 4 нодах с нуля: $\le \$39/\text{мес}$ (в рамках бюджета $\$50/\text{мес}$).

## 3. Возврат Valkey 8 и масштабирование Central API (`> 500 RPS`)
* **Триггер:** Устойчивый пиковый Write RPS $> 500$ или необходимость запуска $\ge 2$ воркеров Uvicorn на Central VPS.
* **Реализация:**
  * Добавление контейнера `valkey:8-alpine` (256 МБ RAM).
  * Перенос `SessionStore` (атомарный `GETDEL test_session:{test_id}`), `TokenBucketLimiter` (Lua-скрипт) и `HeatmapCache` из `central/state.py` в Valkey.
  * Защита фонового `scheduler` распределённой блокировкой `pg_try_advisory_lock(42)` для исключения Split-Brain между воркерами.

## 4. Аналитическое хранилище ClickHouse (`> 50 млн строк/день`)
* **Триггер:** Превышение 50 млн строк в сутки или выход за пределы NVMe диска / IOPS одного экземпляра PostgreSQL 16.
* **Реализация:**
  * Перевод сырой телеметрии `probe_measurements` на асинхронную батчевую запись в ClickHouse (`MergeTree`, партиционирование по месяцам, TTL 365 дней).
  * PostgreSQL 16 остаётся компактным OLTP-хранилищем для справочников (`carriers`, `asn_mapping`, `probe_nodes`, `h3_res6_districts`), `daily_salts` и `outage_incidents`.