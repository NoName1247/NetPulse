  # МОДУЛЬ 1: Деконструкция плана, Gap-анализ и Математика нагрузок (Capacity Planning)

  ## 1.1. Инженерный аудит исходного плана (Gap-анализ и слепые зоны)

  Исходный концепт краудсорсингового сетевого монитора имел ряд критических архитектурных слепых зон и уязвимостей, которые были устранены в ультралёгкой архитектуре **Lean MVP (3 контейнера, 0 Valkey, 0 AI)**:

  1. **Протокол замера сетевых метрик (Day-1 MVP vs Phase 2):**
    * *Проблема:* В исходном плане предлагалось слать «мусорные пакеты» поверх HTTP/TCP, что маскирует потери пакетов в джиттер (TCP retransmissions), либо разворачивать `aiortc` (WebRTC DataChannel), требующий SDP-сигналинга, STUN/TURN, открытых диапазонов UDP-портов и тяжёлых нативных библиотек с риском утечек памяти и OOM на микросерверах. При этом схема *stop-and-wait* при замере скорости искусственно упирается в лимит BDP ($64\text{ КБ} / 0.04\text{ с} = 12.8\text{ Мбит/с}$ на 5G), нули `\x00` в буфере сжимаются Caddy/Brotli до 0 байт (вызывая ложные «10 Гбит/с»), а отправка 2 МБ вслепую крашит медленные каналы ($< 3.35\text{ Мбит/с}$).
    * *Инженерное решение (Day-1 MVP):* Переход на надёжный и высокоточный протокол внутри **одного открытого WebSocket-соединения** `/probe/ws?token=<probe_token>`:
      1. **Гибридный WSS:** Текстовый JSON используется для управляющих сообщений (`hello`, `dl_mark`, `dl_ack`, `ul_go`, `ul_end`, `receipt`), а бинарные фреймы — строго для 3-байтных пингов и 64 КБ чанков. Сжатие WebSocket отключено (`--ws-per-message-deflate false`). Чанки генерируются из несжимаемых псевдослучайных байтов `os.urandom(65536)`.
      2. **Pipelined Server-Initiated Ping-Pong:** Нода шлёт 12 бинарных пингов с интервалом $0.15\text{ с}$ (фаза занимает $\le 3.0\text{ с}$ суммарно для MY и EU). При 5 пропущенных пингах срабатывает Early Abort. Нода сама фиксирует RTT P50/P95, RFC 3550 Jitter и Stall Ratio.
      3. **2-этапный адаптивный Streaming Download с Final ACK:**
          * *Этап 2A (Калибровка 256 КБ, таймаут 8 с):* корректно замеряет скорость от $0.25\text{ Мбит/с}$ (3G/шейпинг). Если канал медленный ($< 2.5\text{ Мбит/с}$ или длительность $\ge 0.8\text{ с}$), калибровки достаточно.
          * *Этап 2B (Быстрый канал):* досылается от 512 КБ до 2 МБ под окно $\sim 1.2\text{ с}$. Замер завершается маркером и клиентским Final ACK, исключая искажения `SO_SNDBUF` ядра Linux и честно измеряя 5G на $300+\text{ Мбит/с}$.
      4. **Bounded Upload:** Клиент шлёт до 16 чанков по 64 КБ (макс. 1 МБ или 1.5 с, либо 256 КБ при медленном канале), скорость считается принимающей нодой.
      5. **Гарантированный выпуск чека:** Даже при обрыве или таймауте нода всегда возвращает подписанный чек с зафиксированной деградацией, устраняя «ошибку выжившего».
    * *План развития:* Чистый UDP Packet Loss % на базе минимального нативного бинарника на Rust (`webrtc-rs` / raw UDP-сокетах без кодеков и FFmpeg) вынесен в **`docs/scale-roadmap.md` (Phase 2)**. Это экономит 2 недели разработки Day-1 MVP и исключает риски нестабильности `aiortc`.

  2. **Парадокс «нет интернета — не открыть сайт» (Full Signal, No Internet):**
    * *Проблема:* Если у пользователя деградировал или упал интернет, тяжелый SPA-сайт (2–5 МБ) не загрузится. Попытка решить это через сложный Service Worker + `IndexedDB` + `Background Sync` наталкивается на фундаментальное ограничение: **Background Sync API не поддерживается в iOS Safari** (WebKit), что ломает UX у 40%+ мобильных пользователей.
    * *Инженерное решение:* Отказ от переусложнённого Background Sync. Вместо этого — сверхлёгкий статический фронтенд **$< 80\text{ КБ}$** (чистый HTML/CSS + Preact/Alpine.js, сжатие Brotli), размещённый на **Cloudflare Pages ($0)**. Благодаря глобальной сети Cloudflare Edge (PoP в Куала-Лумпуре) сайт открывается даже при экстремальной деградации канала на скорости от 50 Кбит/с.

  3. **Эффект «Thundering Herd» (Шторм трафика во время реального сбоя):**
    * *Проблема:* Сервисы мониторинга сбоев имеют низкую фоновую нагрузку, но при крупной аварии у Maxis или Unifi трафик подскакивает в **$30\times\text{–}50\times$ за 2 минуты**. Полноразмерный speedtest на бюджетном VPS мгновенно забьёт сетевой порт, исчерпает лимит трафика и перегрузит CPU.
    * *Инженерное решение:*
      * **Adaptive Bounded Probe:** замер скорости строго ограничен (макс. 2 МБ DL + 1 МБ UL или 1.5 секунды — что наступит раньше) и выполняется **только к ближайшей ноде MY-KUL**.
      * **Dynamic Load Shedding (режим `PULSE_ONLY`):** при загрузке ноды $> 50$ активных сессий нода автоматически отключает throughput-тест и выполняет только быстрый WSS ping-pong + 256 КБ микро-проверку ($\sim 0.27\text{ МБ}$). В базу для `dl_kbps` и `ul_kbps` записывается `NULL`, чтобы не искажать перцентили скорости нулевыми значениями.
      * Фронтенд и тепловые карты отдаются через **Cloudflare Edge Cache ($0)** с политикой `stale-while-revalidate`.

  4. **Искажение данных: Wi-Fi, CGNAT и VPN/Private Relay:**
    * *Проблема:* Пользователь с мобильным оператором Maxis, подключённый к домашнему Wi-Fi от Unifi, получит IP из ASN Telekom Malaysia (`AS4788`). Операторы Малайзии массово используют **CGNAT** (сотни абонентов делят один публичный IPv4), а VPN и iCloud Private Relay подменяют реального провайдера.
    * *Инженерное решение:* Классификация IP по локальной базе ASN (`MaxMind GeoLite2 ASN` в RAM + маппинг малайзийских операторов MCMC/MYNIC) на 4 класса: `MOBILE_CARRIER`, `FIXED_BROADBAND`, `VPN_DATACENTER`, `UNKNOWN`. Трафик с `VPN_DATACENTER` диагностируется для пользователя с явным предупреждением, но **жёстко исключается из агрегации карт операторов**. CGNAT-safe Rate Limiting разделяет лимиты для фиксированных и мобильных подсетей.

  5. **Юридический риск — Закон Малайзии PDPA 2010 (Act 709) и поправки 2024 года:**
    * *Проблема:* Сбор статического IP и точных координат GPS квалифицируется регулятором Малайзии как обработка косвенных персональных данных, требующая согласия и сложного комплаенса. Хранение суточной соли в RAM/Redis создаёт риск потери соли при рестарте кэша, а использование UTC для смены суток приводит к тому, что в 00:00 UTC (08:00 утра в Куала-Лумпуре) соль меняется в разгар утреннего часа пик.
    * *Инженерное решение (Privacy-by-Design):* **Полный отказ от хранения raw IP и точных GPS-координат в БД.**
      1. Суточная соль хранится в таблице PostgreSQL `daily_salts` с привязкой строго к таймзоне **`Asia/Kuala_Lumpur`** (`salt_day = (NOW() AT TIME ZONE 'Asia/Kuala_Lumpur')::date`) и кэшируется в памяти Python-процесса на 5 минут. Соль генерируется и удаляется в **00:05 MYT**.
      2. Raw IP $\rightarrow$ резолв в `ASN / Carrier_ID` + вычисление `session_hash = HMAC-SHA256(IP, daily_salt)`.
      3. Точные GPS-координаты $\rightarrow$ немедленное квантование в гексагон **Uber H3 Resolution 8** (площадь $\approx 0.74\text{ км}^2$, сторона $\approx 460\text{ м}$). Исходные координаты сразу стираются из памяти.

  6. **Защита от отравления данных и архитектура передачи телеметрии:**
    * *Проблема:* Центральный сервер не должен доверять метрикам, сгенерированным в браузере (риск накрутки фейковых сбоев через curl/ботов). При этом построение сложной межнодовой сети (WireGuard Mesh, локальные буферы, очереди Redis Streams) избыточно для MVP и создаёт операционные риски. Клиент также может попытаться подменить `probe_mode` в JSON.
    * *Инженерное решение:* Архитектурный паттерн **Server-Attested Signed Receipts (Канонический HMAC-SHA256)**:
      1. Клиент проходит Turnstile и вызывает `POST /v1/tests/init`. Central API сохраняет метаданные в in-memory словарь `test_session:{test_id}` (TTL 90s) и выдаёт подписанный `probe_token`, привязанный к `test_id`, `node_id`, `probe_mode` и `expires_unix`.
      2. Браузер подключается напрямую к нодам замера MY и EU по WebSocket `/probe/ws?token=<probe_token>`. Нода проверяет токен и фиксирует `test_id` в `seen_test_ids` (TTL 65s) для защиты от повторного использования токена (DoS).
      3. Каждая нода выполняет замер, формирует каноническую строку из **целочисленных значений** (включая `test_id` и `probe_mode`, где `dl_kbps = NULL` сериализуется как `"N"`), подписывает её секретным ключом ноды и возвращает клиенту. Поддерживается ротация секретов `"new_secret,old_secret"`.
      4. Клиент отправляет оба чека одним запросом `POST /v1/tests/complete` на Central API (вместе с опциональным массивом `failed_nodes`, если к одной из нод не удалось подключиться).
      5. Central API атомарно извлекает сессию через `test_session.pop(test_id, None)` (одновременно защищает от Replay-атаки и даёт проверенные метаданные для БД), проверяет подписи нод и сохраняет замер.
      6. Разделение статистических порогов: для тепловой карты достаточно $N \ge 5$ за 24 часа (с бейджами `LOW 5–14 / MED 15–49 / HIGH 50+`), а для детектора аварий требуется $N \ge 15$ за последние 15 минут.

  7. **Максимальное упрощение серверного контура (Lean 3-Container Stack):**
    * **1 воркер Uvicorn (`--workers 1 --loop uvloop`):** Устраняет необходимость в Valkey/Redis на Day-1. 1 асинхронный воркер легко обслуживает до 1 000+ RPS при пиковой нагрузке шторма 35–42 RPS. Все сессии (`test_session`), rate-limiting (Token Bucket) и кэш 24 ключей карт живут в памяти процесса ($< 15\text{ МБ RAM}$).
    * **Устранение Split-Brain и блокировок:** Поскольку воркер ровно один, фоновые задачи (`scheduler`) выполняются прямо внутри него без необходимости `pg_try_advisory_lock(42)`.
    * **Обычная таблица PostgreSQL 16 (без партиций и без Materialized Views):** Замеры пишутся в единую таблицу `probe_measurements` с ведущим индексом `(ts DESC, node_id, carrier_id)`. Выборка по индексу занимает $< 5\text{ ms}$, что делает Materialized Views ненужными.
    * **Pull-Healthcheck Control Plane с контролем Clock Skew:** Центральный сервер опрашивает ноды лёгким pull-запросом `GET /healthz` каждые 5 сек (timeout 2s), контролируя расхождение системных часов (`server_time` drift $> 5\text{s}$ вызывает алерт). Межнодовой push-трафик отсутствует.
    * **Backup Pipeline с клиентским шифрованием `age`:** Потоковый `pg_dump` шифруется публичным ключом утилиты `age` прямо в пайпе и отправляется в Cloudflare R2 каждые 6 часов. Приватный ключ хранится оффлайн.

  ---

  ## 1.2. Нефункциональные требования (NFR) и Математика нагрузок (Back-of-the-Envelope Calculations)

  Расчёт выполнен для масштаба **Year 1 в Малайзии (Klang Valley + крупные агломерации)** с учётом пиковой нагрузки во время телеком-аварий ($50\times$ Outage Storm).

  ### А. Трафик и RPS (Read / Write)
  * **Аудитория:**
    * MVP / Пилот (1–4 мес): $\text{DAU} = 1\,000$, $\text{MAU} = 8\,000$.
    * Целевой масштаб (Year 1): $\text{DAU} = 20\,000$, $\text{MAU} = 150\,000$.
  * **Паттерн поведения (на 1 DAU в сутки):**
    * $1.5$ запуска комплексной диагностики (каждый запуск = 1 запрос `init` + 1 запрос `complete` с обоими чеками $\Rightarrow 2$ write-запроса на запуск, итого $3.0$ write-запроса/DAU).
    * $6$ read-запросов (загрузка тепловой карты H3, статус операторов, активные сбои).
  * **Средний RPS:**
    $$\text{Write RPS}_{\text{avg}} = \frac{20\,000 \times 3.0}{86\,400} = \frac{60\,000}{86\,400} \approx 0.69\text{ RPS}$$
    $$\text{Read RPS}_{\text{avg}} = \frac{20\,000 \times 6}{86\,400} = \frac{120\,000}{86\,400} \approx 1.39\text{ RPS}$$
  * **Пиковый RPS:**
    * Обычный дневной пик ($k_{\text{normal}} = 5$): $\text{Write} \approx 3.5\text{ RPS}$, $\text{Read} \approx 7.0\text{ RPS}$.
    * **Аварийный шторм ($k_{\text{storm}} = 50$, падение Maxis/Unifi в КЛ):**
      $$\text{Write RPS}_{\text{storm}} \approx 35\text{ RPS} \quad (\approx 17\text{ новых диагностик/сек})$$
      $$\text{Read RPS}_{\text{storm}} \approx 70\text{ RPS} \quad (\text{99.99% отдаётся из Cloudflare Edge CDN и RAM})$$
  * **Одновременные активные тесты на нодах (Закон Литтла $L = \lambda \cdot W$):**
    При средней длительности теста $W = 6\text{ сек}$ и интенсивности $\lambda = 17\text{ тестов/сек}$:
    $$L_{\text{storm}} = 17 \times 6 \approx 102\text{ одновременных активных сессий на флот нод}$$

  ### Б. Пропускная способность сети (Bandwidth)
  Разделяем **Control Plane (Центральный сервер)** и **Data Plane (Ноды замера)**.

  1. **Central Server (Control Plane + Heatmap API):**
    * Средний размер Read-ответа (тепловая карта Малайзии Res 6 в Brotli): $8\text{–}15\text{ КБ}$.
    * Средний размер Write-запроса (`init` / `complete` с подписанными чеками): $1.5\text{ КБ}$.
    * В штормовом пике (без учёта CDN — худший случай полного промаха кэша):
      $$\text{BW}_{\text{central\_peak}} = (70 \times 15\text{ КБ} + 35 \times 1.5\text{ КБ}) \times 8 \approx 8.8\text{ Мбит/с}$$
    * *Вывод:* 1 Гбит/с порт центрального VPS имеет запас $> 100\times$.

  2. **Probe Nodes (Data Plane — замер задержки и пропускной способности):**
    * **Обычный режим (`FULL_PROBE`):**
      * WSS Ping-Pong: $12\text{ раундов} \times 3\text{ Б} \times 2 \approx 0.07\text{ КБ}$.
      * Bounded Streaming Throughput: калибровка $256\text{ КБ} + \text{burst} \le 1.75\text{ МБ DL} + \le 1\text{ МБ UL} \le 3\text{ МБ}$ (только к MY-ноде!).
      * К зарубежной ноде (EU-FRA): WSS Ping-Pong + 256 КБ микро-тест $= 0.26\text{ МБ}$.
      * Итого на запуск: нода MY $\approx 3.01\text{ МБ}$, нода EU $\approx 0.26\text{ МБ}$.
      * Месячный трафик (при $30\,000$ тестов/сутки):
        * Нода MY: $30\,000 \times 3.01\text{ МБ} \times 30 \approx 2.71\text{ ТБ/мес}$ (в пределах 4 ТБ лимита VPS).
        * Нода EU: $30\,000 \times 0.26\text{ МБ} \times 30 \approx 234\text{ ГБ/мес}$ (микроскопический расход трафика!).
    * **Штормовой режим (`PULSE_ONLY` — автодеградация при $> 50$ сессий):**
      * Только WSS Ping-Pong + мини-проверка $256\text{ КБ} = 0.26\text{ МБ/тест}$.
      * В шторме ($17\text{ тестов/сек}$): $\text{BW}_{\text{node\_storm}} = 17 \times 0.26\text{ МБ} \times 8 \approx 35.4\text{ Мбит/с}$ на ноду. Канал гарантированно защищён от насыщения.

  ### В. Хранение данных (Storage) и IOPS
  * **Размер 1 строки сырого замера (`probe_measurements`):**
    * Поля: `test_id` (16B), `node_id` (2B), `ts` (8B), `carrier_id` (2B), `asn` (4B), `net_class` (2B), `h3_res8` (8B), `h3_res6` (8B), `geo_source` (2B), `probe_mode` (2B), `rtt_p50_ms` (4B), `rtt_p95_ms` (4B), `jitter_ms` (4B), `stall_ratio` (4B), `dl_kbps` (4B), `ul_kbps` (4B), `session_hash` (16B) + заголовок кортежа PostgreSQL (24B) $\approx 117\text{ байт}$.
    * С индексами (PK по `test_id, node_id` + составной индекс по `ts DESC, node_id, carrier_id`, $\times 2.2$): $\approx 260\text{ байт/строку}$.
  * **Объём данных в чистом PostgreSQL 16 (единая таблица):**
    * При $30\,000$ тестов/день $\times 2.0$ записи (MY + EU) $\approx 60\,000$ строк/день:
      * Прирост в сутки: $60\,000 \times 260\text{ Б} \approx 15.6\text{ МБ/сутки}$.
      * Прирост в месяц: $\approx 470\text{ МБ/мес}$.
      * Ретеншн сырых замеров 180 дней (удаление батчами по 50k ночью): **$\approx 2.8\text{ ГБ}$**.
      * Таблицы `daily_salts`, `h3_res6_districts`, `outage_incidents`: **$< 10\text{ МБ}$**.
      * Ротируемые Docker-логи системы: **$\le 0.5\text{ ГБ}$**.
      * **Итого суммарно на диске Central VPS:** **$< 4\text{ ГБ}$**. На 80 ГБ NVMe VPS остаётся запас свободного места свыше $95\%$.
  * **Требования к IOPS:**
    * Прямая синхронная вставка через пул `asyncpg` даёт нагрузку **$< 25\text{ write IOPS}$** в норме и **$< 120\text{ write IOPS}$** в аварийном шторме. Ведущий индекс по `ts DESC` исключает Sequential Scan. Обычный NVMe VPS ($3\,000+$ IOPS) имеет запас более $25\times$.

  ### Г. Метрики задержки (Latency SLA) и Надёжность
  | Метрика / Слой | Целевой показатель (SLA) | Примечание |
  | :--- | :--- | :--- |
  | **Heatmap & Status Read API** | P50 $\le 20\text{ ms}$, P95 $\le 60\text{ ms}$, P99 $\le 120\text{ ms}$ | Отдача из Cloudflare Edge CDN / RAM FastAPI |
  | **Test Init (`POST /v1/tests/init`)** | P50 $\le 30\text{ ms}$, P95 $\le 80\text{ ms}$, P99 $\le 150\text{ ms}$ | GeoLite2 lookup в RAM + запись в `test_session` dict |
  | **Full Diagnostic E2E** | $4.5\text{–}6.5\text{ s}$ (Pulse: $2.0\text{–}2.5\text{ s}$) | Единый WebSocket-сеанс (Ping + DL + UL) |
  | **Rule-Based Diagnostic Verdict** | **P50 $\le 3\text{ ms}$, P95 $\le 8\text{ ms}$, P99 $\le 15\text{ ms}$** | Мгновенный детерминированный движок (0 LLM, $0 cost) |
  | **Availability (Read / Heatmaps)** | **99.9%** ($\le 43.8\text{ мин/мес}$) | Cloudflare Stale-While-Revalidate |
  | **Availability (Probe Fleet)** | **99.95%** (автодеградация при отказе ноды) | Исключение упавшей ноды за $\le 15\text{ сек}$ |
  | **RPO / RTO** | **RPO = 6 часов** / **RTO = 30 минут** | Зашифрованный `pg_dump` с `age` в Cloudflare R2 |

  ---

  # МОДУЛЬ 2: Архитектурные решения (ADR) и Топология системы (C4 + Mermaid)

  ## 2.1. Архитектурный стиль

  Выбран архитектурный стиль: **«Синхронный Модульный Монолит (Python 3.12 / FastAPI, 1 worker) + Stateless Edge Probe Nodes (DNS-Only) + Signed Receipts + In-Memory Сессии/Кэш + Прямой PostgreSQL (3 контейнера на Central VPS, 0 Valkey)»**.

  * **Почему 1 воркер Uvicorn на Central Server?** При штормовой нагрузке $35\text{–}42\text{ RPS}$ запуск двух воркеров приводит к Split-Brain (необходимости внешнего Valkey/Redis для хранения `test_session` и синхронизации фоновых задач через advisory lock). Один процесс на `uvloop` гарантированно держит $1\,000+\text{ RPS}$, позволяет хранить активные сессии в обычном словаре `dict` с атомарным `pop()` и устраняет целый контейнер из архитектуры.
  * **Почему распределённые Stateless Probe Nodes (DNS-Only)?** Замер качества каналов подчиняется законам сетевой топологии: невозможно измерить задержку до Европы или аварию на подводном кабеле с одного сервера в Куала-Лумпуре. Ноды замера полностью **stateless** (не содержат БД), работают в режиме **DNS-Only (без проксирования Cloudflare)** для замера реального сквозного маршрута от клиента.
  * **Взаимодействие с нодами (Pull vs Push):** Ноды не отправляют телеметрию в центр (push-трафик и WireGuard отсутствуют). Единственное служебное взаимодействие — лёгкий pull-опрос от лидер-воркера `scheduler` раз в 5 секунд: `GET https://{node}/healthz` (timeout 2s), возвращающий `{"status":"ok","mode":"FULL_PROBE","active_sessions":12,"server_time":1764087000}` для обновления списка живых нод в памяти и контроля Clock Skew.
  * **Почему Signed Receipts вместо WireGuard Mesh и Redis Streams?** Вместо постоянной VPN-сети ноды замера сами подписывают измеренный результат каноническим секретным ключом (HMAC-SHA256). Клиент передаёт оба чека (MY и EU) в Central API одним HTTP-запросом `POST /v1/tests/complete`. Это исключает межнодовой трафик, упрощает инфраструктуру до нуля и устраняет единую точку отказа брокера.

  ---

  ## 2.2. Таблица ADR (Architecture Decision Records)

  | Компонент / Узел | Выбранная технология | Альтернативы | Обоснование выбора (Плюсы + CAP/PACELC) | Компромиссы и риски (Trade-offs) |
  | :--- | :--- | :--- | :--- | :--- |
  | **Edge Proxy / WAF / CDN** | **Cloudflare Free** + **Caddy v2** на VPS | Nginx, Envoy, AWS CloudFront | Бесплатный L3/L4/L7 DDoS-щит, Turnstile (антибот), кэширование статики и тепловых карт на edge-узлах в КЛ (KUL). Caddy на VPS даёт авто-TLS 1.3, проверку `X-Origin-Secret` и Unix domain socket к FastAPI. | Привязка к Cloudflare; на Free-тарифе ограничены правила WAF (5 правил — достаточно для защиты API). |
  | **Frontend Hosting** | **Cloudflare Pages** ($0) | Vercel, Netlify, Self-hosted Nginx | Глобальная раздача статики $< 80\text{ КБ}$ (HTML/CSS + Preact/Alpine.js, Brotli). Открывается даже на 50 Кбит/с при аварии сети. | Статический хостинг без SSR (для легковесного клиента замера SSR не требуется). |
  | **Backend & Probe Engine** | **Python 3.12 + FastAPI + Uvicorn (1 worker, uvloop) + Hybrid WSS** | Rust (Axum), Go (Fiber), Node.js | Основной язык команды — **Python**. 1 воркер держит 1 000+ RPS; исключает Split-Brain и распределённые блокировки; `test_session` и кэш живут в памяти процесса ($< 15\text{ МБ}$). | Python уступает Rust/Go по CPU при шифровании. Риск нивелируется режимом `PULSE_ONLY` при перегрузке. Rust-сайдкар для UDP packet loss запланирован на Phase 2. |
  | **Протокол замера** | **Pipelined WSS Ping + 2-этапный Streaming WSS Download + WSS Upload** | WebRTC DataChannel (`aiortc`), HTTP Stop-and-Wait | Кросс-браузерная работа; устраняет лимит BDP на 5G (300+ Мбит/с) и искажение `SO_SNDBUF`; ранний аборт при потере связи; точные RTT P50/P95, Jitter, Stall Ratio. | Не измеряет чистый UDP packet loss на уровне сокетов (вынесено в Phase 2). |
  | **Основная БД** | **Чистый PostgreSQL 16 (Обычная таблица без партиций и MatViews)** | TimescaleDB, PostGIS, ClickHouse, MongoDB | **PACELC: PC/EC**. Единая таблица `probe_measurements` с ведущим индексом `(ts DESC, node_id, carrier_id)`. Прямые запросы за 15 мин и 24 ч выполняются за $< 5\text{ ms}$. 0 хрупких партиций, 0 блокировок MatView. | Раз в сутки ночью удаляются данные старше 180 дней батчами по 50k строк. При росте $> 50\text{ млн}$ строк/день сырые логи выносятся в ClickHouse. |
  | **Пространственная индексация** | **Uber H3 (Res 6 и 8) + справочник `h3_res6_districts`** | PostGIS ST_Cluster, S2 Geometry, GeoHash | H3-гексагоны хранятся как `BIGINT` (8 байт). Таблица `h3_res6_districts` связывается через `LEFT JOIN` (без жёсткого FK), поэтому замеры за пределами КЛ не падают. | Границы H3 не совпадают точно с административными границами районов КЛ $\rightarrow$ маппинг в `h3_res6_districts`. |
  | **Кэш и Rate-Limit** | **In-Memory dict в процессе FastAPI (< 15 MB) + Cloudflare Edge** | Valkey, Redis, Memcached | Полный отказ от Valkey на MVP! 24 страновых ключа карты кэшируются на 30 сек в RAM и Cloudflare Edge (Hit Rate $> 99.9\%$). Токены и сессии гасятся атомарным `dict.pop()`. | При рестарте FastAPI стираются активные 90-секундные сессии тестов (пользователь просто перезапустит тест). При $> 500\text{ RPS}$ Valkey возвращается по `docs/scale-roadmap.md`. |
  | **Определение провайдера (IP $\rightarrow$ ASN)** | **MaxMind GeoLite2 ASN/City (локальный `.mmdb` в RAM)** + MY Telco ASN | Внешние платные API (ipdata, ipinfo) | Нулевая задержка ($< 0.1\text{ ms}$), **$0/мес**, 100% приватность (IP никогда не уходят сторонним сервисам — строго по PDPA). | Базу `.mmdb` нужно обновлять по cron 2 раза в неделю. Мобильный GeoIP неточен в районах $\rightarrow$ opt-in Browser Geolocation. |
  | **Межнодовое взаимодействие** | **Signed Receipts (HMAC-SHA256)** через браузер | WireGuard Mesh + HTTPS POST, gRPC | Избавляет от необходимости поддерживать WireGuard-туннели между нодами; каноническая строка связывает `test_id` и `probe_mode`; поддержка ротации ключей `"new,old"`. | Риск сокрытия «хорошего» чека нивелируется Turnstile, суточной дедупликацией и статистическим порогом публикации $N \ge 5$ (res 6) / $N \ge 15$ (аварии). |
  | **Резервное копирование** | **Cron `pg_dump` $\rightarrow$ шифрование `age` $\rightarrow$ Cloudflare R2** | pgBackRest WAL streaming, AWS S3 | Потоковый дамп шифруется публичным ключом утилиты `age` прямо в пайпе. Приватный ключ хранится оффлайн. 0 байт на диске VPS, $0 egress, 10 ГБ бесплатно в R2. | RPO = 6 часов (для сервиса мониторинга телеком-сбоев это полностью адекватно и исключает риск остановки БД из-за переполнения WAL). |

  ---

  ## 2.3. Диаграммы архитектуры (Mermaid.js)

  ### А. C4 Container Diagram (Lean MVP — 3 контейнера на Central VPS, 0 Valkey, 0 AI)

  ```mermaid
  graph TB
      User["Lightweight Web Client (<80KB)<br/>HTML/CSS + Preact/Alpine.js<br/>(Hosted on Cloudflare Pages - $0)"]

      subgraph Edge["Cloudflare Free (KUL PoP)"]
          CF["WAF + DDoS + Turnstile<br/>CDN Cache (Heatmap TTL 30s)<br/>Header: X-Origin-Secret"]
      end

      subgraph CentralVPS["Central VPS (2 vCPU, 4GB RAM, 80GB NVMe) ~$16/mo"]
          CaddyC["Caddy v2 (64MB)<br/>TLS 1.3 + nftables CF IP Whitelist"]
          App["Python 3.12 FastAPI App (384MB)<br/>1 Uvicorn Worker (uvloop, 0 Split-Brain):<br/>Init/Complete API, Rule Engine, daily_salt cache (5m),<br/>In-Memory Sessions (90s) & Token Bucket (<15MB),<br/>Outage Check (2m), Pull Healthcheck (5s)"]
          GeoDB[("Local GeoLite2 .mmdb<br/>(mmap in RAM - Zero IP Storage)")]
          PG[("Vanilla PostgreSQL 16 (1.5GB)<br/>Single probe_measurements table (ts DESC index),<br/>daily_salts (MYT), outage_incidents<br/>(asyncpg pool: 10 conn, no PgBouncer, no MatViews)")]
      end

      subgraph ProbeNodes["Stateless Probe Nodes (DNS-Only, NO Cloudflare Proxy, NO WireGuard)"]
          NodeMY["Probe Node 1: MY-KUL (Existing, is_local=TRUE)<br/>Caddy + FastAPI Probe Daemon<br/>Pipelined Ping + 2-Stage Adaptive WSS DL/UL"]
          NodeEU["Probe Node 2: EU-FRA (Existing, is_local=FALSE)<br/>Caddy + FastAPI Probe Daemon<br/>Pipelined Ping + 256KB WSS Pulse Check"]
      end

      subgraph Ops["Managed Free Ops ($0/mo)"]
          R2[("Cloudflare R2 (<=10GB)<br/>Encrypted pg_dump with age every 6h")]
          Monitor["Sentry Free (Errors) +<br/>Uptime Kuma -> Telegram Alerts"]
      end

      User -- "HTTPS REST (Init, Complete, Heatmap)" --> CF
      CF -- "HTTPS TLS 1.3" --> CaddyC
      CaddyC -- "Unix Socket" --> App

      User -- "Direct WSS /probe/ws (Token-gated)" --> NodeMY
      User -- "Direct WSS /probe/ws (Token-gated)" --> NodeEU
      NodeMY -. "Returns Canonical HMAC Receipt" .-> User
      NodeEU -. "Returns Canonical HMAC Receipt" .-> User

      App -- "Pull Healthcheck (5s GET /healthz)" --> NodeMY
      App -- "Pull Healthcheck (5s GET /healthz)" --> NodeEU

      App -- "Local mmap" --> GeoDB
      App -- "TCP / asyncpg" --> PG
      PG -- "Cron pg_dump | age | rclone (6h)" --> R2
      App -. "Error Stacktraces" .-> Monitor
  ```

  ---

  ### Б. Sequence Diagram: Диагностика через Signed Receipts + Rule-Based Вердикт

  ```mermaid
  sequenceDiagram
      autonumber
      actor User as Пользователь (Web Client <80KB)
      participant CF as Cloudflare Edge + Turnstile
      participant App as Central FastAPI (1 Worker)
      participant PG as PostgreSQL 16
      participant NodeMY as Probe Node MY (is_local=TRUE)
      participant NodeEU as Probe Node EU (is_local=FALSE)

      User->>CF: POST /v1/tests/init (Turnstile Token, опц. GPS)
      CF->>App: Forward + CF-Connecting-IP + X-Origin-Secret
      App->>App: Check In-Memory Token Bucket
      alt Rate Limit превышен
          App-->>User: 429 Too Many Requests + Retry-After
      else Лимит OK
          App->>App: RAM Lookup: IP -> ASN/Carrier (GeoLite2)<br/>GPS -> H3 Res 8 & Res 6; session_hash = HMAC(IP, daily_salt)<br/>(Raw IP & GPS немедленно уничтожаются!)
          App->>App: In-Memory test_session[test_id] = {carrier, asn, h3, exp: now+90}
          App-->>User: 200 OK: {test_id, probe_tokens:[MY, EU], nodes:[MY(FULL), EU(PULSE)], network_info}
      end

      par Параллельный замер к MY и EU нодам (по прямому WSS /probe/ws?token=...)
          User->>NodeMY: WSS Connect (/probe/ws?token=probe_token)
          NodeMY->>NodeMY: Validate HMAC probe_token, anti-DoS check (claim test_id 65s)
          NodeMY->>User: JSON {"type":"hello", "actual_mode":"FULL_PROBE", "ping_count":12}
          NodeMY->>User: Pipelined Ping (12 бинарных фреймов по 3 байта, 150ms)
          User-->>NodeMY: Pong-ответы (замер RTT P50/P95, Jitter, Stall)
          alt actual_mode == FULL_PROBE
              NodeMY->>User: Stage 2A: 256KB Calibration DL + {"type":"dl_mark", "stage":1}
              User-->>NodeMY: {"type":"dl_ack", "bytes":262144}
              alt Канал быстрый (> 2.5 Mbps)
                  NodeMY->>User: Stage 2B: Burst DL (512KB..2MB) + {"type":"dl_mark", "stage":2}
                  User-->>NodeMY: {"type":"dl_ack", "bytes":burst_bytes} (Final ACK, меряет 300+ Mbps 5G)
              end
              NodeMY->>User: {"type":"ul_go", "max_bytes":1048576, "max_ms":1500}
              User->>NodeMY: Streaming UL chunks (up to 1MB) + {"type":"ul_end"}
          end
          NodeMY-->>User: JSON {"type":"receipt", metrics..., "signature_hmac":sig}
      and
          User->>NodeEU: WSS Connect (/probe/ws?token=probe_token, PULSE_ONLY)
          NodeEU->>NodeEU: Validate HMAC probe_token
          NodeEU->>User: JSON {"type":"hello", "actual_mode":"PULSE_ONLY"}
          NodeEU->>User: Pipelined Ping (12 фреймов) + 256KB Pulse Check (dl_kbps=NULL)
          alt Нода EU недоступна / обрыв зарубежного кабеля
              NodeEU--xUser: Timeout 3s
              Note over User: Фиксирует failed_nodes = [{"node_id":2, "error":"TIMEOUT"}]
          else Нода EU ответила
              NodeEU-->>User: JSON {"type":"receipt", metrics..., "signature_hmac":sig}
          end
      end

      User->>CF: POST /v1/tests/complete ({test_id, receipts:[MY], failed_nodes:[EU]})
      CF->>App: Forward
      App->>App: session = test_session.pop(test_id, None) (атомарная защита от Replay!)
      alt Сессия не найдена (Replay / истек TTL)
          App-->>User: 403 Forbidden (Invalid or Expired Session)
      else Сессия валидна
          App->>App: Проверка канонических HMAC-подписей (целые числа, test_id + actual_mode)
          alt Подпись невалидна или режим подделан
              App-->>User: 403 Forbidden (Tampered Receipt)
          else Подписи валидны
              App->>PG: INSERT INTO probe_measurements (test_id, node_id, carrier_id, asn, net_class, h3_res8, h3_res6, probe_mode, dl_kbps=NULL for PULSE, metrics...) (~2ms)
              App->>PG: SELECT stats FROM probe_measurements WHERE h3_res6 = ... AND ts >= NOW()-15m AND node_id IN (local) (~3ms)
              App->>App: Deterministic Rule Engine (MY metrics + EU failed check + District Stats -> Verdict JSON)
              App-->>User: 200 OK: Полный отчёт + вердикт (без Polling, без SSE, без LLM!)
          end
      end
  ```

  ---

  # МОДУЛЬ 3: Серверная архитектура, API-контракты и Асинхронность (Backend Deep Dive)

  ## 3.1. Сетевой слой, Edge-периметр и Протоколы взаимодействия

  Принцип сетевой изоляции: **жёсткое разделение Control Plane (через Cloudflare WAF/CDN) и Data Plane (прямое WSS-подключение к Probe-нодам мимо CDN)**.

  1. **Control Plane Perimeter (Central VPS — 3 контейнера):**
    * **Cloudflare Free (KUL PoP):** L3/L4/L7 DDoS-защита, WAF-правила, **Cloudflare Turnstile** (невидимая капча перед запуском теста), TLS 1.3, сжатие **Brotli**.
    * **Origin Proxy — Caddy v2 (на Central VPS):**
      * Межсетевой экран **`nftables`** блокирует порт 443 для всех входящих IP-адресов, кроме официальных подсетей Cloudflare (`curl https://www.cloudflare.com/ips-v4`).
      * В Caddy настроена проверка заголовка `X-Origin-Secret`, который Cloudflare Transform Rules автоматически подставляет к каждому проксируемому запросу. Прямой обход Cloudflare невозможен.
      * Проксирование на FastAPI идёт через **Unix Domain Socket** (`/run/app/uvicorn.sock`) без оверхеда TCP loopback.
      * Uvicorn запускается с 1 воркером: `uvicorn main:app --uds /run/app/uvicorn.sock --loop uvloop --http httptools`.

  2. **Data Plane Perimeter (Probe Nodes: MY-KUL, EU-FRA, далее SG, JP):**
    * **DNS-Only (Grey Cloud):** Домены нод (`my-node.domain.com`, `eu-node.domain.com`) работают в режиме **DNS-Only (без проксирования Cloudflare)**. Это гарантирует измерение реального сетевого маршрута абонента без промежуточного Anycast-узла Cloudflare.
    * На каждой ноде развёрнут лёгкий стек: **Caddy v2** (TLS 1.3 Let's Encrypt) + **FastAPI Probe Daemon**.
    * Команда запуска Uvicorn на нодах:
      ```bash
      uvicorn main:app --host 0.0.0.0 --port 8000 --ws websockets --ws-per-message-deflate false --ws-max-size 131072
      ```
    * В Caddy для `/probe/ws` **не** включается сжатие, чтобы избежать искусственного завышения пропускной способности.
    * Открыт только один порт: **TCP 443**. Никаких диапазонов WebRTC UDP портов (`40000-40500`) и никаких WireGuard-интерфейсов на нодах не требуется.

  3. **Матрица протоколов взаимодействия:**

  | Канал / Сценарий | Протокол и формат | Обоснование выбора |
  | :--- | :--- | :--- |
  | **Инициализация теста, завершение теста, тепловые карты, активные сбои** | **REST / OpenAPI 3.1** (JSON + Brotli поверх HTTP/2 & HTTP/3) | Стандартный легковесный транспорт; `GET /v1/heatmap` кэшируется на Cloudflare Edge с TTL 30s. |
  | **Служебный мониторинг нод (Pull Healthcheck)** | **HTTP/2 GET https://{node}/healthz** (Timeout 2s) | Каждые 5 сек опрашивает ноды; нода отдаёт `status`, `mode`, `active_sessions`, `server_time`. Межнодовой push отсутствует. |
  | **Замер сетевых метрик (Ping, Jitter, Stall, DL, UL)** | **Гибридный WebSocket (WSS)** `/probe/ws?token=<probe_token>` | 1) Текстовый JSON для команд. 2) Pipelined Ping (12 фреймов по 3B). 3) 2-этапный DL с Final ACK. 4) Bounded UL. 0 оверхеда TLS. |
  | **Аттестация замеров (Anti-Poisoning)** | **Канонический Signed Receipt (HMAC-SHA256)** | Нода подписывает целочисленную каноническую строку секретным ключом. Привязка к `test_id` и `probe_mode`. Поддержка ротации `"new,old"`. |

  ---

  ### Ключевые API-контракты

  #### 1) `POST /v1/tests/init` (Инициализация сессии замера)
  *Заголовки:* `X-Turnstile-Token: <token>`, `Idempotency-Key: <uuidv7>`

  ```json
  // Request Body
  {
    "client_geo": { "lat": 3.0554, "lon": 101.7005 }, // опционально (opt-in)
    "connection_hint": { "effective_type": "4g" }       // из Network Information API (если доступен)
  }

  // Response 200 OK
  {
    "test_id": "019a9f4c-7b2a-7c10-9e44-1f8a2b3c4d5e",
    "expires_at": "2026-11-25T16:10:00Z",
    "detected_network": {
      "asn": 9534,
      "carrier_slug": "maxis",
      "carrier_name": "Maxis / Hotlink",
      "net_class": "MOBILE_CARRIER", // MOBILE_CARRIER | FIXED_BROADBAND | VPN_DATACENTER | UNKNOWN
      "exclude_from_heatmap": false,
      "h3_res8": "886506666bfffff",  // сырые координаты уничтожены!
      "h3_res6": "866506667ffffff",
      "district_name": "Bukit Jalil",
      "geo_source": "BROWSER_GPS"    // BROWSER_GPS | GEOIP_COARSE
    },
    "probe_plan": [
      {
        "node_id": 1,
        "region": "MY-KUL",
        "wss_url": "wss://my-node.netpulse.my/probe/ws",
        "probe_mode": "FULL_PROBE",  // FULL_PROBE | PULSE_ONLY
        "probe_token": "019a9f4c-7b2a-7c10-9e44-1f8a2b3c4d5e|1|FULL_PROBE|1764087060|hmac_hex"
      },
      {
        "node_id": 2,
        "region": "EU-FRA",
        "wss_url": "wss://eu-node.netpulse.my/probe/ws",
        "probe_mode": "PULSE_ONLY",  // к зарубежной ноде всегда PULSE_ONLY
        "probe_token": "019a9f4c-7b2a-7c10-9e44-1f8a2b3c4d5e|2|PULSE_ONLY|1764087060|hmac_hex"
      }
    ]
  }
  ```
  *Действие сервера:* сохраняет метаданные в in-memory словарь `test_session[test_id]` (TTL 90s) с распознанными `carrier_id`, `asn`, `net_class`, `h3_res8`, `h3_res6`, `geo_source`, `session_hash` и `expected_nodes`.

  #### 2) `POST /v1/tests/complete` (Приём подписанных чеков и выдача вердикта)
  *Заголовки:* `Content-Type: application/json`

  ```json
  // Request Body
  {
    "test_id": "019a9f4c-7b2a-7c10-9e44-1f8a2b3c4d5e",
    "receipts": [
      {
        "node_id": 1,
        "probe_mode": "FULL_PROBE",
        "measured_at": 1764087005,
        "metrics": {
          "rtt_p50_ms": 28.4,
          "rtt_p95_ms": 42.1,
          "jitter_ms": 4.2,
          "stall_ratio": 0.0,
          "dl_kbps": 12500,
          "ul_kbps": 3200
        },
        "signature_hmac": "a3f5c88b9012d9e8..."
      }
    ],
    "failed_nodes": [
      {
        "node_id": 2,
        "error": "TIMEOUT"
      }
    ]
  }

  // Response 200 OK (выдаётся синхронно за < 15 ms!)
  {
    "test_id": "019a9f4c-7b2a-7c10-9e44-1f8a2b3c4d5e",
    "carrier": "Maxis / Hotlink",
    "district": "Bukit Jalil",
    "verdict_code": "INTL_ROUTE_DEGRADED",
    "verdict_text_ru": "Локальная сеть в Малайзии работает стабильно (28.4 ms), но зарубежный маршрут до Европы недоступен — вероятен обрыв международного подводного кабеля.",
    "verdict_text_en": "Local network in Malaysia is stable (28.4 ms), but international routing to Europe timed out — likely a submarine cable or overseas gateway issue.",
    "verdict_text_bm": "Rangkaian tempatan di Malaysia beroperasi dengan stabil (28.4 ms), tetapi laluan antarabangsa ke Eropah terputus — kemungkinan masalah kabel dasar laut.",
    "metrics": {
      "local_rtt_p50_ms": 28.4,
      "intl_rtt_p50_ms": null,
      "dl_mbps": 12.5,
      "ul_mbps": 3.2,
      "stall_ratio": 0.0
    },
    "district_comparison": {
      "district_health_score": 88,
      "status": "HEALTHY",
      "samples_24h": 46
    }
  }
  ```

  #### 3) `GET /v1/heatmap?carrier=maxis&window=24h&res=6` (Страновая тепловая карта без `bbox`)
  *Заголовки ответа:* `Cache-Control: public, max-age=30, stale-while-revalidate=60`
  Эндпоинт отдаёт все активные ячейки Малайзии за выбранный период (объём в Brotli всего $5\text{–}20\text{ КБ}$). Это генерирует всего **12–24 уникальных ключа кэша на всю страну** (6 операторов $\times$ 2 окна $\times$ 2 разрешения), обеспечивая **99.99% Hit Rate на Cloudflare Edge CDN**!

  ```json
  {
    "window": "24h",
    "carrier": "maxis",
    "resolution": 6,
    "min_samples": 5,
    "cells": [
      {
        "h3": "866506667ffffff",
        "district": "Cheras",
        "samples": 64,
        "confidence": "HIGH", // HIGH (N>=50), MED (15<=N<50), LOW (5<=N<15)
        "avg_stall_ratio": 0.01,
        "p95_rtt_ms": 45.2,
        "median_dl_kbps": 14200,
        "health_score": 89,
        "status": "HEALTHY"
      }
    ]
  }
  ```

  #### 4) `GET /v1/incidents/active` (Активные сбои в Малайзии)
  ```json
  {
    "active_incidents": [
      {
        "id": 104,
        "carrier_slug": "unifi",
        "carrier_name": "Unifi (Telekom Malaysia)",
        "district_name": "Cheras",
        "h3_res6": "866506667ffffff",
        "severity": "MAJOR_OUTAGE",
        "verdict_code": "DISTRICT_CARRIER_OUTAGE",
        "unique_reports": 38,
        "avg_stall_ratio": 0.28,
        "p95_rtt_ms": 240.5,
        "started_at": "2026-11-25T15:30:00Z",
        "last_seen_at": "2026-11-25T16:02:00Z"
      }
    ]
  }
  ```

  ---

  ## 3.2. Модульная декомпозиция (DDD Bounded Contexts)

  На Central VPS развёрнуто единое приложение (**FastAPI Модульный Монолит, 1 воркер**) с тремя изолированными пакетами внутри одного контейнера `app`:

  | Модуль | Bounded Context (Зона ответственности) | Вход / Выход | Масштабирование |
  | :--- | :--- | :--- | :--- |
  | **`diagnostics`** | Обработка `POST /v1/tests/init` (сохранение в `test_session`, выдача токенов) и `POST /v1/tests/complete` (атомарный `test_session.pop`, сверка канонического HMAC с поддержкой ротации `new,old`, прямая вставка в `probe_measurements`, учёт `failed_nodes`). | Вход: REST от браузера.<br/>Выход: план замера, вердикт. | **Stateless.** 1 Uvicorn воркер обслуживает до 1 000+ RPS. |
  | **`analytics`** | Прямой `SELECT` из `probe_measurements` с фильтром `is_local = TRUE` (по индексу `ts DESC`), страновая тепловая карта без `bbox` (кэш в RAM 30s), детерминированный Rule Engine для вердикта замера (с учётом шейпинга `dl_kbps < 1500` и сбоев `failed_nodes`), расчёт байесовского Health Score. | Вход: HTTP GET запросы.<br/>Выход: JSON тепловых карт и инцидентов. | **In-Memory Cache (30s)** + Cloudflare Edge (30s). |
  | **`scheduler`** | Встроенный фоновый цикл `asyncio` внутри единственного воркера: 1) SQL-детектор массовых сбоев каждые 2 мин (автооткрытие и автозакрытие `resolved_at`); 2) Pull Healthcheck нод каждые 5 сек (`GET /healthz`) с контролем Clock Skew ($> 5\text{s}$ алерт); 3) удаление старых солей из `daily_salts` в 00:05 MYT; 4) ночная очистка замеров старше 180 дней батчами по 50k; 5) обновление GeoLite2 2 раза в неделю. | Вход: системный таймер.<br/>Выход: обновлённые инциденты, алерты. | **In-Process Cron** (0 Split-Brain, блокировки не требуются). |
  | **`probe_daemon`** *(на нодах)* | Сверхлёгкий сервис на нодах замера: валидация `probe_token`, анти-DoS словарь `seen_test_ids` (65s), первый фрейм `hello`, Pipelined Ping (12 фреймов), 2-этапный WSS Download с Final ACK (защита от `SO_SNDBUF` и крашей на медленном канале), Bounded WSS Upload, выпуск чека даже при обрыве, ответ на `GET /healthz`. | Вход: WSS от браузера + HTTP от Central API.<br/>Выход: подписанный JSON-чек клиенту, healthz статус. | **Stateless Edge Probe.** Масштабируется добавлением серверов за $0–5/мес. |

  ---

  # МОДУЛЬ 4: Архитектура Данных, Хранилищ и Кэширования (Data & Storage Layer)

  ## 4.1. Консолидированное хранение данных

  Стек хранилищ сокращён до 3 базовых проверенных компонентов:

  1. **Vanilla PostgreSQL 16** — OLTP, суточные соли `daily_salts` (таймзона MYT), справочники операторов и `h3_res6_districts`, единая таблица замеров `probe_measurements` (с ведущим индексом `(ts DESC, node_id, carrier_id)`), таблица инцидентов `outage_incidents`. **Никаких сторонних расширений, никаких партиций и никаких Materialized Views.**
  2. **In-Memory Cache в процессе FastAPI (< 15 MB RAM)** — сессии тестов `test_session` (TTL 90s), Token Bucket rate-limiter, кэш 24 страновых ключей тепловых карт (TTL 30s), список живых нод.
  3. **Cloudflare R2** — объектное S3-хранилище для зашифрованных утилитой `age` резервных копий `pg_dump` каждые 6 часов (Free Tier: 10 ГБ бесплатно, $0 за исходящий трафик).

  ---

  ### Диаграмма сущностей (ERD в Mermaid)

  ```mermaid
  erDiagram
      CARRIERS ||--o{ ASN_MAPPING : "owns ASNs"
      CARRIERS ||--o{ PROBE_MEASUREMENTS : "measured on"
      CARRIERS ||--o{ OUTAGE_INCIDENTS : "suffers"
      PROBE_NODES ||--o{ PROBE_MEASUREMENTS : "executes probe"
      H3_RES6_DISTRICTS ||--o{ PROBE_MEASUREMENTS : "district name (LEFT JOIN)"
      H3_RES6_DISTRICTS ||--o{ OUTAGE_INCIDENTS : "district name (LEFT JOIN)"

      DAILY_SALTS {
          date salt_day PK "Asia/Kuala_Lumpur date"
          bytea salt "32 random bytes"
      }

      CARRIERS {
          smallint id PK
          varchar slug UK "maxis, celcomdigi, umobile, unifi, time, yes"
          varchar display_name
          smallint carrier_type "1=Mobile, 2=Fixed"
      }

      ASN_MAPPING {
          int asn PK "e.g. 9534, 4788, 38466"
          smallint carrier_id FK
          smallint net_class "1=Mobile, 2=Fixed, 3=VPN/DC, 4=Unknown"
          varchar org_name
      }

      PROBE_NODES {
          smallint id PK
          varchar region_code UK "MY-KUL, EU-FRA, SG-SIN"
          varchar endpoint_url
          boolean is_local "TRUE for MY, FALSE for EU/SG"
          boolean is_active
      }

      H3_RES6_DISTRICTS {
          bigint h3_res6 PK "Uber H3 Res 6 (~3.2km edge)"
          varchar district_name "Cheras, Kepong, Bukit Jalil, KLCC"
          varchar state_name "W.P. Kuala Lumpur, Selangor"
      }

      PROBE_MEASUREMENTS {
          uuid test_id PK
          smallint node_id PK, FK
          timestamptz ts "Indexed with node_id & carrier_id"
          smallint carrier_id FK
          int asn
          smallint net_class "1=Mobile, 2=Fixed, 3=VPN, 4=Unknown"
          bigint h3_res8
          bigint h3_res6
          smallint geo_source "1=BrowserGPS, 2=GeoIP"
          smallint probe_mode "1=FULL_PROBE, 2=PULSE_ONLY"
          real rtt_p50_ms
          real rtt_p95_ms
          real jitter_ms
          real stall_ratio "0.0 .. 1.0"
          int dl_kbps "NULL for PULSE_ONLY"
          int ul_kbps "NULL for PULSE_ONLY"
          bytea session_hash "HMAC(IP, daily_salt) - NO RAW IP"
      }

      OUTAGE_INCIDENTS {
          bigserial id PK
          smallint carrier_id FK
          bigint h3_res6 "Indexed, NO rigid FK"
          varchar severity "DEGRADED | MAJOR_OUTAGE"
          varchar verdict_code "DISTRICT_CARRIER_OUTAGE, etc."
          int avg_stall_bps
          real p95_rtt_ms
          int unique_reports
          timestamptz started_at
          timestamptz last_seen_at
          timestamptz resolved_at "NULL = active, NOT NULL = auto-closed"
      }
  ```

  ---

  ### Обновлённый чистый SQL DDL (`central/schema.sql`)

  ```sql
  -- 1. Суточная соль (таймзона строго Asia/Kuala_Lumpur!)
  CREATE TABLE daily_salts (
      salt_day DATE PRIMARY KEY,
      salt     BYTEA NOT NULL
  );

  -- 2. Справочник операторов и ASN
  CREATE TABLE carriers (
      id           SMALLSERIAL PRIMARY KEY,
      slug         VARCHAR(32) UNIQUE NOT NULL,
      display_name VARCHAR(64) NOT NULL,
      carrier_type SMALLINT NOT NULL -- 1=Mobile, 2=Fixed
  );

  CREATE TABLE asn_mapping (
      asn        INT PRIMARY KEY,
      carrier_id SMALLINT REFERENCES carriers(id),
      net_class  SMALLINT NOT NULL, -- 1=Mobile, 2=Fixed, 3=VPN/DC, 4=Unknown
      org_name   VARCHAR(120) NOT NULL
  );

  -- 3. Реестр Probe-нод с явным флагом is_local (MY vs EU не смешиваются!)
  CREATE TABLE probe_nodes (
      id           SMALLINT PRIMARY KEY,
      region_code  VARCHAR(16) UNIQUE NOT NULL, -- 'MY-KUL', 'EU-FRA', 'SG-SIN'
      endpoint_url VARCHAR(128) NOT NULL,
      is_local     BOOLEAN NOT NULL DEFAULT FALSE,
      is_active    BOOLEAN NOT NULL DEFAULT TRUE
  );

  -- 4. Справочник районов (используется через LEFT JOIN, без жёсткого FK)
  CREATE TABLE h3_res6_districts (
      h3_res6       BIGINT PRIMARY KEY,
      district_name VARCHAR(80) NOT NULL,
      state_name    VARCHAR(60) NOT NULL
  );

  -- 5. Единая таблица замеров (без партиций + честный PK (test_id, node_id))
  CREATE TABLE probe_measurements (
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
  CREATE INDEX idx_pm_ts_valid ON probe_measurements (ts DESC, node_id, carrier_id)
      WHERE net_class IN (1, 2);

  -- 6. Инциденты (без жёсткого FK на h3_res6 -> не падает в Пенанге/Джохоре!)
  CREATE TABLE outage_incidents (
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
  CREATE UNIQUE INDEX idx_outage_active_uniq
      ON outage_incidents (carrier_id, h3_res6)
      WHERE resolved_at IS NULL;
  ```

  ---

  ### Аналитические SQL-запросы (`central/analytics.py` и `central/scheduler.py`)

  #### 1. Тепловая карта без `bbox` (строго по локальным нодам `is_local = TRUE`, порог $N \ge 5$)
  ```sql
  SELECT
      m.h3_res6,                                          -- или m.h3_res8 при res=8
      COALESCE(d.district_name, 'Malaysia') AS district_name,
      COUNT(DISTINCT m.session_hash)        AS unique_sessions,
      AVG(m.stall_ratio)                    AS avg_stall_ratio,
      PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY m.rtt_p95_ms) AS p95_rtt_ms,
      PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY m.dl_kbps)    AS median_dl_kbps -- NULL игнорируется!
  FROM probe_measurements m
  JOIN probe_nodes n ON n.id = m.node_id AND n.is_local = TRUE
  LEFT JOIN h3_res6_districts d ON d.h3_res6 = m.h3_res6
  WHERE m.ts >= NOW() - $1::interval                      -- '24 hours' (дефолт) | '1 hour' | '7 days'
    AND m.net_class IN (1, 2)
    AND ($2::smallint IS NULL OR m.carrier_id = $2)
  GROUP BY m.h3_res6, d.district_name
  HAVING COUNT(DISTINCT m.session_hash) >= 5;             -- LOW: 5-14 | MED: 15-49 | HIGH: 50+
  ```

  #### 2. Детектор сбоев (раз в 2 мин) с автооткрытием И автозакрытием `resolved_at`
  ```sql
  -- Шаг A: Найти и открыть/обновить активные локальные сбои (строго is_local = TRUE, N >= 15)
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
      GROUP BY m.carrier_id, m.h3_res6
      HAVING COUNT(DISTINCT m.session_hash) >= 15
        AND (
            AVG(m.stall_ratio) >= 0.15
            OR PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY m.rtt_p95_ms) > 220
            OR PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY m.dl_kbps) < 1500
        )
  )
  INSERT INTO outage_incidents (carrier_id, h3_res6, severity, verdict_code, avg_stall_bps, p95_rtt_ms, unique_reports)
  SELECT
      carrier_id, h3_res6,
      CASE WHEN avg_stall_bps >= 2500 OR uniq_users >= 30 THEN 'MAJOR_OUTAGE' ELSE 'DEGRADED' END,
      'DISTRICT_CARRIER_OUTAGE', avg_stall_bps, cur_p95, uniq_users
  FROM current_bad
  ON CONFLICT (carrier_id, h3_res6) WHERE resolved_at IS NULL
  DO UPDATE SET
      severity       = EXCLUDED.severity,
      avg_stall_bps  = EXCLUDED.avg_stall_bps,
      p95_rtt_ms     = EXCLUDED.p95_rtt_ms,
      unique_reports = EXCLUDED.unique_reports,
      last_seen_at   = NOW();

  -- Шаг B: Автозакрытие инцидентов, которые не подтверждались последние 20 минут!
  UPDATE outage_incidents
  SET resolved_at = NOW()
  WHERE resolved_at IS NULL
    AND last_seen_at < NOW() - INTERVAL '20 minutes';
  ```

  #### 3. Ротация соли и ночная очистка по времени Куала-Лумпура (MYT)
  ```sql
  -- Получение/создание соли на сегодня в MYT (UTC+8)
  INSERT INTO daily_salts (salt_day, salt)
  VALUES ((NOW() AT TIME ZONE 'Asia/Kuala_Lumpur')::date, $1)
  ON CONFLICT (salt_day) DO NOTHING;

  -- Удаление вчерашних солей в 00:05 MYT
  DELETE FROM daily_salts
  WHERE salt_day < (NOW() AT TIME ZONE 'Asia/Kuala_Lumpur')::date;

  -- Ночная очистка замеров старше 180 дней батчами по 50k (без блокировок)
  DELETE FROM probe_measurements
  WHERE ctid IN (
      SELECT ctid FROM probe_measurements
      WHERE ts < NOW() - INTERVAL '180 days'
      LIMIT 50000
  );
  ```

  ---

  # МОДУЛЬ 5: Детерминированный движок диагностики, статистики и детекции сбоев (Rule-Based & Statistical Engine)

  Весь тяжёлый, нестабильный и дорогой AI/LLM-контур **полностью исключён из архитектуры**. Вместо него внедрён компактный, прозрачный и надёжный **Deterministic Rule-Based & Statistical Engine**. Он выполняется за **$< 2\text{ ms}$**, потребляет **$0\text{ MB}$ GPU VRAM**, стоит **$\$0/\text{мес}$** и даёт **0% галлюцинаций**.

  ---

  ## 5.1. Матрица детерминированной диагностики маршрута (MY vs EU + Район)

  При вызове `POST /v1/tests/complete` сервер выполняет функцию `evaluate_verdict(my_receipt, eu_receipt, failed_nodes, h3_stats, net_class)`.

  ### Определение деградации локального замера (`my_degraded`)
  ```python
  my_degraded = (
      my.stall_ratio > 0.15
      or my.rtt_p95_ms > 150.0
      or (my.probe_mode == "FULL_PROBE" and my.dl_kbps is not None and my.dl_kbps < 1500)
  )
  ```

  ### Обработка сбоев зарубежного направления (`failed_nodes`)
  Если локальный замер к MY успешен (`my_degraded == False`), но зарубежная нода EU вернула ошибку в `failed_nodes` (`TIMEOUT` / `UNREACHABLE`) или `eu.stall_ratio > 0.15` $\rightarrow$ немедленно выносится вердикт **`INTL_ROUTE_DEGRADED`**.

  ### Таблица правил и готовые шаблоны вердиктов (RU / EN / BM)

  | Условие правила | `verdict_code` | Что видит пользователь (готовые шаблоны RU / EN / BM) |
  | :--- | :--- | :--- |
  | `net_class == VPN_DATACENTER` | `VPN_ACTIVE` | **RU:** *«Обнаружен VPN/Proxy. Замер выполнен, но реальный оператор не определён и данные не учтены в карте покрытия.»*<br/>**EN:** *“VPN/Proxy connection detected. Test completed, but carrier is unidentified and data is excluded from public coverage maps.”*<br/>**BM:** *“Sambungan VPN/Proksi dikesan. Ujian selesai, tetapi pembekal perkhidmatan sebenar tidak dikenal pasti dan data dikecualikan daripada peta liputan.”* |
  | `MY: OK` и `EU: Degraded/Timeout` (в чеке или в `failed_nodes`) | `INTL_ROUTE_DEGRADED` | **RU:** *«Локальная сеть в Малайзии работает стабильно (`{my_rtt} ms`), но фиксируется деградация международного маршрута до Европы — возможны проблемы на зарубежном шлюзе/подводном кабеле.»*<br/>**EN:** *“Local network in Malaysia is stable ({my_rtt} ms), but international routing to Europe is degraded — likely an overseas gateway or submarine cable issue.”*<br/>**BM:** *“Rangkaian tempatan di Malaysia beroperasi dengan stabil ({my_rtt} ms), tetapi laluan antarabangsa ke Eropah terjejas — kemungkinan masalah pintu masuk antarabangsa atau kabel dasar laut.”* |
  | `MY: Degraded` (`my_degraded == True`) **И** в гексагоне `unique_sessions >= 15` с `district_health < 50` | `DISTRICT_CARRIER_OUTAGE` | **RU:** *«Подтверждён массовый сбой **{carrier}** в районе **{district}** (подтверждено {N} пользователями за последний час). Это проблема на стороне оператора, не вашего устройства.»*<br/>**EN:** *“Mass outage confirmed for {carrier} in {district} ({N} users confirmed in the past hour). This is an operator-side issue, not your device.”*<br/>**BM:** *“Gangguan besar disahkan untuk {carrier} di {district} (disahkan oleh {N} pengguna dalam sejam yang lalu). Ini adalah masalah pihak syarikat telekomunikasi, bukan peranti anda.”* |
  | `MY: Degraded`, но в гексагоне `unique_sessions >= 15` и `district_health >= 75` | `LOCAL_DEVICE_OR_INDOOR` | **RU:** *(Если сработал только шейпинг `dl_kbps < 1500`):* *«Зафиксировано сильное ограничение скорости ({dl_mbps} Mbps) при стабильном пинге — вероятна перегрузка базовой станции или шейпинг тарифа.»*<br/>*(В остальных случаях):* *«У других пользователей **{carrier}** в **{district}** связь стабильна. Проблема, вероятно, локальная (слабый сигнал в здании, перегруженный Wi-Fi-роутер или ограничения тарифа).»*<br/>**EN:** *“Other {carrier} users in {district} have stable connection. Issue is likely local (weak indoor signal, congested Wi-Fi router, or account throttled).”*<br/>**BM:** *“Pengguna {carrier} lain di {district} mempunyai sambungan yang stabil. Masalah mungkin setempat (isyarat dalam bangunan lemah, Wi-Fi sesak, atau had pelan).”* |
  | `MY: Degraded`, а в гексагоне `unique_sessions < 15` | `DEGRADED_LOW_SAMPLE` | **RU:** *«Зафиксировано ухудшение связи (`{my_rtt} ms`, `{my_dl} Mbps`), но по району **{district}** пока недостаточно замеров для подтверждения массовой аварии ({N}/15).»*<br/>**EN:** *“Connection degradation detected ({my_rtt} ms, {my_dl} Mbps), but insufficient samples in {district} to confirm mass outage ({N}/15).”*<br/>**BM:** *“Kemerosotan sambungan dikesan ({my_rtt} ms, {my_dl} Mbps), tetapi sampel tidak mencukupi di {district} untuk mengesahkan gangguan besar ({N}/15).”* |
  | `MY: OK` и `EU: OK` | `NETWORK_HEALTHY` | **RU:** *«Соединение **{carrier}** в районе **{district}** стабильно по локальным и международным маршрутам.»*<br/>**EN:** *“Connection {carrier} in {district} is stable on both local and international routes.”*<br/>**BM:** *“Sambungan {carrier} di {district} adalah stabil pada kedua-dua laluan tempatan dan antarabangsa.”* |

  ---

  ## 5.2. Формула `Health Score (0..100)` и Разделённые пороги публикации

  1. **Оценка одного гексагона по сырым метрикам (Raw Score):**
    $$\text{Score}_{\text{raw}} = 100 - \min\big(50,\ 250 \cdot \text{avg\_stall\_ratio}\big) - \min\big(30,\ \max(0,\ \frac{\text{p95\_rtt\_ms} - 50}{6})\big) - \min\big(20,\ \max(0,\ \frac{10000 - \text{COALESCE}(\text{median\_dl\_kbps}, 5000)}{500})\big)$$

  2. **Байесовское сглаживание (Bayesian Smoothing):**
    $$\text{HealthScore} = \frac{N \cdot \text{Score}_{\text{raw}} + m \cdot C}{N + m} \quad (C = 75,\ m = 15)$$

  3. **Разделение статистических порогов:**
    * **Публичная тепловая карта (`window=24h` или `7d`):**
      * $N < 5$: ячейка не отображается (нет данных).
      * $5 \le N < 15$: бейдж **`LOW CONFIDENCE`** (показывает общее направление качества).
      * $15 \le N < 50$: бейдж **`MED CONFIDENCE`**.
      * $N \ge 50$: бейдж **`HIGH CONFIDENCE`**.
    * **Детектор массовых аварий (`window=15m`):** строго $N \ge 15$ уникальных пользователей с деградацией в районе Res 6.

  ---

  # МОДУЛЬ 6: Инфраструктура, Оркестрация, Observability и FinOps

  ## 6.1. Топология инфраструктуры и Ресурсы Central VPS (3 контейнера)

  В архитектуре **Lean MVP** на Central VPS работают строго **3 контейнера** (Valkey вырезан полностью, сессии и кэш карт живут в RAM процесса FastAPI):

  | Контейнер | CPU Limit | RAM Limit | Факт. потребление | Назначение |
  | :--- | :---: | :---: | :---: | :--- |
  | `caddy` | `0.2` | `96 MB` | $\sim 35\text{ MB}$ | TLS 1.3, проверка `X-Origin-Secret`, Brotli, Unix domain socket |
  | `app` (FastAPI, **1 worker**) | `0.8` | `384 MB` | **$\sim 120\text{ MB}$** | API + GeoLite2 + Rule Engine + `daily_salt` кэш + in-memory сессии/кэш ($<15\text{ MB}$) + cron |
  | `postgres:16` | `1.0` | `1536 MB` | $\sim 800\text{ MB}$ | Чистый PG 16 без партиций и MatViews (`shared_buffers=512MB`, `max_connections=30`, asyncpg 10 conn) |
  | ~~`valkey:8`~~ | — | — | **Удалён на MVP** | Перенесён в `docs/scale-roadmap.md` на случай роста $> 500\text{ RPS}$ |
  | **ИТОГО** | — | **2.02 ГБ лимит** | **~955 МБ факт** | **Свыше 3.0 ГБ свободной RAM под Page Cache ОС. Риск OOM = 0.** |

  ### Конфигурация Probe Nodes (MY, EU — 1 vCPU, 1 ГБ RAM каждая)
  * На каждой ноде запущены только 2 контейнера:
    1. `caddy` (`64 MB RAM` лимит, $\sim 25\text{ MB}$ факт): TLS 1.3, авто-сертификаты Let's Encrypt.
    2. `probe-daemon` (`128 MB RAM` лимит, $\sim 45\text{ MB}$ факт): приём WSS `/probe/ws`, Pipelined Ping, 2-этапный DL с Final ACK, каноническая подпись HMAC-SHA256, эндпоинт `GET /healthz`.

  ### CI/CD пайплайн (Zero-Downtime, Cost = $\$0$)
  * **GitHub Actions** (при `push` в `main`):
    1. Линтинг и тесты: `ruff check` + `pytest` (unit + API contract tests).
    2. Сборка Docker-образов (`python:3.12-slim`).
    3. Публикация в **GHCR (GitHub Container Registry, $0)**.
    4. Деплой по SSH на Central VPS: `docker compose pull && docker compose up -d --remove-orphans`.
  * **Секреты:** передаются через GitHub Actions Secrets в файл `.env` на сервере с правами `chmod 600`.

  ---

  ## 6.2. Observability и Мониторинг

  1. **Sentry Free Tier:** Автоматически перехватывает все необработанные исключения с полным контекстом запроса.
  2. **Uptime Kuma $\rightarrow$ Telegram:**
    * Пингует Central VPS (`/healthz`) и ноды (`/healthz`) каждые 30 секунд.
    * Контроль Clock Skew: если расхождение часов ноды и центра `drift > 5.0s` $\rightarrow$ алерт в Telegram.
    * Магистральный алерт: если $\ge 30$ уникальных пользователей одного оператора за 5 минут сообщили об ошибке (`failed_nodes`) к ноде, которая по `/healthz` остаётся живой $\rightarrow$ предупреждение в Telegram об обрыве пиринга/магистрали оператора.
  3. **Структурированное логирование (Zero-PII Logs):** JSON-логи через `structlog` с фильтром `strip_pii_fields` и ротацией Docker-логов (`max-size: 50m`, `max-file: 3`).

  ---

  ## 6.3. FinOps и Юнит-экономика (Строго $\le \$50/\text{мес}$)

  | Статья расходов | MVP (2 свои ноды MY + EU) | Full Scale (4 ноды с нуля) |
  | :--- | :---: | :---: |
  | Central VPS (2 vCPU, 4 GB RAM, 80 GB NVMe, SG/KL) | $\$16.00$ | $\$16.00$ |
  | Probe Node 1 (`MY-KUL`) + Probe Node 2 (`EU-FRA`) | $\$0.00$ *(уже есть)* | $\$11.50$ |
  | Probe Node 3 (`SG-SIN`) + Probe Node 4 (`JP-TYO`) | — | $\$10.00$ |
  | Cloudflare Pages + Free WAF/CDN/Turnstile + R2 + Sentry + GeoLite2 | $\$0.00$ | $\$0.00$ |
  | Домен (амортизация в месяц) | $\$1.50$ | $\$1.50$ |
  | **ИТОГО OPEX / месяц** *(Потолок бюджета: $\$50.00$)* | **$\$17.50/\text{мес}$** | **$\$39.00/\text{мес}$** |

  * **Себестоимость 1 000 тестов:** $\frac{\$39.00}{900} \approx \mathbf{\$0.043}$ ($4.3$ цента за 1 000 тестов, маржинальная стоимость вердикта = $\$0.00$).

  ---

  # МОДУЛЬ 7: Информационная Безопасность и Анализ Отказов (Security & FMEA)

  ## 7.1. Безопасность (Zero-Trust & Malaysia PDPA Compliance)

  1. **Zero-Account B2C:** Без регистрации, паролей и хранения PII в БД.
  2. **In-Memory Rate Limiting:** Token Bucket прямо в памяти воркера FastAPI (15 тестов / 10 мин на `/24` для Fixed, 120 тестов / 10 мин для Mobile CGNAT).
  3. **Signed Receipts с привязкой `test_id` и `probe_mode`:**
    * Каноническая целочисленная строка исключает расхождения float и подмену режима.
    * In-memory `test_session.pop(test_id, None)` гасит сессию атомарно.
    * `seen_test_ids` (TTL 65s) на нодах отсекает повторный DoS по токену.
    * Поддержка ротации ключей `"new,old"`.
  4. **PDPA Privacy-by-Design:**
    * База `GeoLite2.mmdb` работает в RAM; IP преобразуются в `session_hash = HMAC(IP, daily_salt)`.
    * Соль хранится в PostgreSQL `daily_salts` с привязкой к дате в `Asia/Kuala_Lumpur` и очищается в 00:05 MYT.
    * Точный GPS квантуется в H3 Res 8 ($\approx 0.74\text{ км}^2$), сырые координаты не сохраняются.
  5. **Защита периметра:** `nftables` (только IP Cloudflare) + `X-Origin-Secret` в Caddy + SSH только по ключам Ed25519.
  6. **Клиентское шифрование бэкапов `age`:** Приватный ключ хранится оффлайн.

  ---

  ## 7.2. Таблица анализа отказов (FMEA — Failure Mode and Effects Analysis)

  | Узел / Сбой (Что упало?) | Влияние на систему (Blast Radius) | Вероятность / Критичность | Детектирование (Метрика / Алерт) | Автоматическая реакция системы (Mitigation / Fallback) |
  | :--- | :--- | :---: | :--- | :--- |
  | **1. Отказ `Probe Node MY` (падение VPS или сети в КЛ)** | Недоступен локальный speedtest и пинг; международная диагностика работает. | Средняя / **Высокая** | 3 пропущенных `GET /healthz` ($> 15\text{s}$) в Node Registry; алерт Uptime Kuma в Telegram. | Central API за $\le 15\text{ сек}$ исключает `MY-KUL` из `/v1/tests/init`. **На этапе MVP (2 ноды):** тест автоматически деградирует к замеру только через `EU-FRA` с флагом `local_probe_unavailable=true` в отчёте. **После добавления `SG-SIN` (Phase 2):** локальный трафик прозрачно переключается на резервную ноду в Сингапуре (~8–12 мс от КЛ) с пометкой `fallback_node=true`. |
  | **2. Отказ зарубежной ноды (`EU-FRA` / `JP-TYO`)** | Невозможно проверить зарубежный маршрут; локальный мониторинг и тепловые карты работают штатно. | Средняя / **Низкая** | Таймаут WSS $> 3\text{ сек}$ на клиенте; клиент шлёт `failed_nodes: [{"node_id":2, "error":"TIMEOUT"}]`. | Сервер выдаёт вердикт `INTL_ROUTE_DEGRADED` (локальная сеть в норме, зарубежный канал деградировал); инцидент на карте не создаётся. При $\ge 30$ таких репортов шлётся алерт о сбое пиринга. |
  | **3. Шторм трафика $50\times$ (массовый сбой Maxis / Unifi в КЛ)** | Риск перегрузки сетевого канала нод и процессоров Central VPS. | **Высокая** / **Критическая** | Счётчик `active_sessions > 50` на нодах; всплеск RPS на Cloudflare. | Автоматический **Load Shedding**: ноды переходят в `PULSE_ONLY` (трафик падает в $11\times$, `dl_kbps` пишется как `NULL`); Cloudflare Edge отдаёт тепловые карты из кэша (`stale-while-revalidate`); Token Bucket в RAM отсекает спам. |
  | **4. Полное падение Central VPS (сбой гипервизора / диска)** | Невозможно запускать новые тесты и сохранять замеры. | Низкая / **Критическая** | Uptime Kuma фиксирует `DOWN` $> 30\text{ сек}$ $\rightarrow$ алерт в Telegram. | **Read Path:** Cloudflare Edge отдаёт закэшированную статику фронтенда и тепловые карты. **DR:** Подъём резервного VPS скриптом + расшифровка приватным ключом `age` бэкапа из Cloudflare R2 (**RPO = 6 ч, RTO $\le 30$ мин**). |
  | **5. Деградация / замедление PostgreSQL** | Задержки при отдаче свежих тепловых карт и сохранении чеков. | Средняя / **Высокая** | Время ответа `complete` $> 500\text{ ms}$; Sentry фиксирует таймауты `asyncpg`. | Пул `asyncpg` сбрасывает зависшие транзакции по `command_timeout = 5s`; пользователи продолжают читать закэшированные карты из RAM и Cloudflare L0; запись возобновляется автоматически. Ведущий индекс `(ts DESC, node_id, carrier_id)` исключает seq scan. |
  | **6. Повторная отправка или подделка Signed Receipt (Replay / Tampering)** | Попытка накрутить фейковый сбой оператору путём многократной отправки одного валидного чека или подмены режима замера. | Средняя / **Высокая** | Рост ответов `403 Invalid or Expired Session` / `403 Tampered Receipt` в Sentry/логах. | Атомарное чтение и удаление `test_session.pop(test_id, None)` в RAM + проверка канонической подписи (связывающей `test_id` и `probe_mode`) + `PRIMARY KEY (test_id, node_id)` с `ON CONFLICT DO NOTHING` в PostgreSQL. Повторный чек отбрасывается мгновенно. |
  | **7. Сбой ночного/6-часового `pg_dump` в Cloudflare R2** | Нет свежей резервной копии (живая БД при этом **не страдает**, в отличие от WAL archiving!). | Низкая / **Средняя** | Cron-скрипт не получил exit code 0 $\rightarrow$ мгновенный алерт в Telegram-бот. | Живая БД продолжает работать штатно; сохраняются предыдущие снапшоты в R2 (ретеншн 14 дней); автоматический повторный запуск скрипта через 15 минут. |
  | **8. Зависание или сбой детектора аварий** | Инциденты не обновляются или не закрываются вовремя. | Низкая / **Низкая** | Таймаут SQL-запроса $> 10\text{s}$ в логах `scheduler` $\rightarrow$ алерт в Sentry. | Выборка идёт по индексу `(ts DESC)` всего за 15 минут ($< 5\text{ ms}$). При следующем тике через 2 мин запрос повторяется; незакрытые сбои старше 20 мин автоматически помечаются `resolved_at = NOW()`. |
  | **9. Атака Data Poisoning (накрутка фейковых сбоев)** | Попытка испортить тепловую карту оператора спам-запросами через ботнет. | Средняя / **Высокая** | Всплеск доли невалидных Turnstile-токенов в Cloudflare. | 1) Метрики принимаются только с канонической HMAC-подписью ноды (подделка в браузере невозможна). 2) Дедупликация по `session_hash` (1 голос на IP в сутки). 3) Отсечение VPN/Datacenter ASN. 4) Для создания аварии требуется $N \ge 15$ уникальных IP за 15 мин в одном районе. |

  ---

  # МОДУЛЬ 8: Эталонный исходный код компонентов (Reference Implementation)

  ## 8.1. Общий протокольный модуль: `shared_proto.py`
  Файл размещается в общем пакете и импортируется как в `central/`, так и в `probe/`.

  ```python
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


  def sign_probe_token(secrets: list[str], test_id: str, node_id: int, mode: str, ttl_s: int = 60) -> str:
      exp = int(time.time()) + ttl_s
      payload = f"{test_id}|{node_id}|{mode}|{exp}"
      return f"{payload}|{_hmac_hex(secrets[0], payload)}"


  def verify_probe_token(secrets: list[str], token: str, expected_node_id: int) -> tuple[str, str] | None:
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
      test_id: str, node_id: int, actual_mode: str, measured_at: int,
      rtt_p50_ms: float, rtt_p95_ms: float, jitter_ms: float,
      stall_ratio: float, dl_kbps: int | None, ul_kbps: int | None,
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
  ```

  ---

  ## 8.2. Демон зонда замера: `probe/main.py`
  Развёртывается на независимых серверах замера (MY-KUL, EU-FRA).

  ```python
  import asyncio
  import json
  import os
  import statistics
  import struct
  import time
  from fastapi import FastAPI, WebSocket, WebSocketDisconnect

  import shared_proto as proto

  NODE_ID = int(os.environ["NODE_ID"])
  NODE_SECRETS = proto.parse_secrets(os.environ["NODE_SECRET"])  # поддерживает "new,old"
  MAX_FULL_SESSIONS = int(os.environ.get("MAX_FULL_SESSIONS", "50"))

  # Несжимаемый случайный буфер 64 КБ (защита от ложного сжатия Brotli в Caddy)
  CHUNK_64K = os.urandom(65536)

  app = FastAPI(docs_url=None, redoc_url=None)
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


  @app.get("/healthz")
  async def healthz():
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
          msg = await asyncio.wait_for(ws.receive(), timeout=rem)
          if msg["type"] == "websocket.disconnect":
              raise WebSocketDisconnect()
          if msg.get("text"):
              try:
                  data = json.loads(msg["text"])
                  if data.get("type") == expected_type:
                      return data
              except json.JSONDecodeError:
                  continue


  async def _run_pipelined_pings(ws: WebSocket) -> tuple[float, float, float, float, bool]:
      # Асинхронный pipelined Ping (<= 3.2s для MY и EU) + Early Abort + жёсткий дедлайн
      sent_ts: dict[int, int] = {}
      rtts_ms: list[float] = []
      aborted = False
      deadline = time.monotonic() + proto.PING_PHASE_BUDGET_S

      async def sender():
          for seq in range(proto.PING_COUNT):
              if aborted or time.monotonic() >= deadline - 0.5:
                  break
              sent_ts[seq] = time.monotonic_ns()
              await ws.send_bytes(struct.pack("!BH", proto.OP_PING, seq))
              await asyncio.sleep(proto.PING_INTERVAL_S)

      async def receiver():
          nonlocal aborted
          while len(rtts_ms) < proto.PING_COUNT:
              rem = deadline - time.monotonic()
              if rem <= 0:
                  break
              try:
                  msg = await asyncio.wait_for(ws.receive(), timeout=min(rem, 0.3))
              except asyncio.TimeoutError:
                  now_ns = time.monotonic_ns()
                  timed_out = sum(
                      1 for s, t0 in sent_ts.items()
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
      recv1 = min(int(ack1.get("bytes", 0)), proto.CAL_DL_BYTES)
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
          return est_kbps, True

      elapsed2 = max(0.02, (t2_end - t2_start) - 0.5 * (rtt_p50_ms / 1000.0))
      recv2 = min(int(ack2.get("bytes", 0)), burst_bytes)
      return max(1, int((recv2 * 8) / (elapsed2 * 1000))), False


  async def _run_bounded_upload(ws: WebSocket, dl_kbps: int | None) -> int | None:
      ul_limit = proto.SLOW_UL_BYTES if (dl_kbps is not None and dl_kbps < 2000) else proto.MAX_UL_BYTES
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
          except asyncio.TimeoutError:
              break
          if msg["type"] == "websocket.disconnect":
              raise WebSocketDisconnect()
          if msg.get("text"):
              try:
                  if json.loads(msg["text"]).get("type") == "ul_end":
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
  async def probe_ws(ws: WebSocket, token: str):
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
          await ws.send_json({
              "type": "hello",
              "node_id": NODE_ID,
              "actual_mode": actual_mode,
              "ping_count": proto.PING_COUNT,
          })

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
              test_id=test_id, node_id=NODE_ID, actual_mode=actual_mode,
              measured_at=measured_at, rtt_p50_ms=rtt_p50, rtt_p95_ms=rtt_p95,
              jitter_ms=jitter, stall_ratio=stall_ratio, dl_kbps=dl_kbps, ul_kbps=ul_kbps,
          )
          await ws.send_json({
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
          })
          await ws.close(code=1000)

      except WebSocketDisconnect:
          pass
      finally:
          active_sessions -= 1
  ```


  netpulse-my/
├── shared_proto.py                  # [из спецификации] Общий крипто/бинарный протокол (Central + Probe)
├── pyproject.toml                   # Зависимости (central, probe, dev), настройки ruff и pytest
├── central/
│   ├── __init__.py
│   ├── main.py                      # Точка входа FastAPI (1 worker), lifespan, роуты, error handlers
│   ├── config.py                    # Pydantic Settings (env, ротация NODE_SECRETS, лимиты)
│   ├── models.py                    # Enums, Pydantic DTO (REST-контракты), внутренние dataclass-ы
│   ├── logging_setup.py             # structlog JSON + strip_pii_fields + Sentry init (before_send)
│   ├── db.py                        # Пул asyncpg (max=10, timeout=5s) + работа с daily_salts (MYT)
│   ├── state.py                     # In-Memory (<15MB): SessionStore, TokenBucket, HeatmapCache, NodeRegistry, FailedNodeTracker
│   ├── geo.py                       # MaxMind GeoLite2 (mmap RAM), ASN/Carrier классификатор, H3 Res 8/6, HMAC session_hash
│   ├── alerts.py                    # Асинхронные алерты в Telegram (Clock Skew, Peering Outage) с антиспам-cooldown
│   ├── analytics.py                 # [из спецификации] Rule Engine (RU/EN/BM), Bayesian Health Score, SQL Heatmap & Incidents
│   ├── diagnostics.py               # Bounded Context: Turnstile, /v1/tests/init, /v1/tests/complete, верификация чеков
│   ├── scheduler.py                 # [из спецификации] In-process asyncio циклы (5s healthz, 2m outages, MYT salt, 180d retention)
│   ├── schema.sql                   # [из спецификации] Vanilla PostgreSQL 16 DDL
│   └── seed.sql                     # Начальные справочники: операторы MY, ASN (MCMC/MYNIC), probe_nodes, районы КЛ H3 Res 6
├── probe/
│   ├── __init__.py
│   └── main.py                      # [из спецификации] Stateless Probe Daemon (/probe/ws, /healthz)
├── frontend/                        # Статика < 80 КБ для Cloudflare Pages ($0)
│   ├── index.html                   # Минимальный HTML + подключение Turnstile + EN/BM/RU переключатель
│   ├── styles.css                   # Компактные стили (mobile-first, без тяжёлых фреймворков)
│   ├── probe-client.js              # Гибридный WSS-клиент (Pipelined Pong, Stage 2A/2B dl_ack, Bounded UL, 3s EU timeout)
│   └── app.js                       # UI диагностики, вердикты, карта H3 / список районов, активные инциденты
├── infra/
│   ├── Dockerfile.central           # python:3.12-slim для Central App
│   ├── Dockerfile.probe             # python:3.12-slim для Probe Daemon
│   ├── docker-compose.central.yml   # 3 контейнера: caddy, app (1 worker UDS), postgres:16 (точные лимиты CPU/RAM)
│   ├── docker-compose.probe.yml     # 2 контейнера: caddy, probe-daemon (без WS compression)
│   ├── caddy/
│   │   ├── Caddyfile.central        # TLS 1.3, X-Origin-Secret, Brotli, Unix socket
│   │   └── Caddyfile.probe          # TLS 1.3, без сжатия на /probe/ws
│   ├── nftables/
│   │   └── central.nft              # Whitelist IP Cloudflare на порт 443 + SSH
│   └── backup/
│       └── backup_to_r2.sh          # Cron: pg_dump | age | rclone -> R2 + Telegram alert on failure
├── .github/
│   └── workflows/
│       └── ci-deploy.yml            # ruff + pytest -> build GHCR -> SSH docker compose pull/up
├── docs/
│   └── scale-roadmap.md             # [из спецификации] Phase 2: Rust UDP loss, SG/JP ноды, Valkey >500 RPS, ClickHouse
└── tests/
    ├── conftest.py                  # Фикстуры FastAPI TestClient, mock asyncpg/GeoIP/Turnstile
    ├── test_shared_proto.py         # Тесты HMAC-токенов, канонических чеков, "N" для NULL, ротации ключей
    ├── test_probe_ws.py             # Тесты WSS: 4403/4409, FULL_PROBE, PULSE_ONLY, early abort, таймауты
    ├── test_rule_engine.py          # Тесты всех веток Rule Engine (RU/EN/BM), Health Score, порогов N
    ├── test_central_api.py          # Контрактные тесты init/complete/heatmap/incidents, Replay, Tampering, 429
    └── test_privacy_and_state.py    # Тесты MYT соли, strip_pii_fields, H3, Token Bucket (Fixed vs CGNAT), Peering Alert