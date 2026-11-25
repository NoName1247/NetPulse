# NetPulse MY

Crowdsourced telecom quality & outage monitor for Malaysia (Lean MVP).

## Architecture

- **Central API (`central/`)**: Single-worker FastAPI + `uvloop` monolith with in-memory sessions/rate-limiting (`TokenBucketLimiter`), `GeoLite2` MMDB ASN/City classification, H3 spatial quantization (Res 8 / Res 6), deterministic RU/EN/BM Rule Engine, and PostgreSQL 16 (`asyncpg`).
- **Probe Nodes (`probe/`)**: Stateless WebSocket probe daemons (`/probe/ws`, `/healthz`) running pipelined binary pings, 2-stage adaptive download, bounded upload, and HMAC-SHA256 signed receipts (`shared_proto.py`).
- **Frontend (`frontend/`)**: Lightweight vanilla HTML/CSS/ES modules UI with Leaflet interactive map and radial quality elevation overlay (red $\leftrightarrow$ green).
- **Infrastructure (`infra/`)**: Dockerfiles, Caddy/Nginx configs, `nftables` firewall template, and encrypted `pg_dump | age -> Cloudflare R2` backup script.

## Quick Start (Docker Compose)

1. Copy environment template and fill secrets:
   ```bash
   cp .env.example .env
   chmod 600 .env
   ```
2. Place `GeoLite2-ASN.mmdb` and `GeoLite2-City.mmdb` into `data/geoip/`.
3. Start the stack:
   ```bash
   docker compose up -d --build
   ```

## Tests

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,probe]"
pytest -v
```
