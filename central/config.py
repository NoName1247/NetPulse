import json
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

import shared_proto as proto


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "production"

    # PostgreSQL 16 (Vanilla, asyncpg pool 10 conn, timeout 5s)
    database_url: str = "postgresql://netpulse:netpulse@postgres:5432/netpulse"
    db_pool_min_size: int = 2
    db_pool_max_size: int = 10
    db_command_timeout_s: float = 5.0

    # Секреты нод в JSON: '{"1":"new_my,old_my","2":"new_eu,old_eu"}'
    node_secrets_json: str = Field(
        default='{"1":"dev-my-secret","2":"dev-eu-secret"}',
    )
    # Опциональные внутренние URL для pull-healthcheck (например при reverse-proxy на одном хосте)
    probe_healthcheck_urls_json: str = ""

    # Локальные базы MaxMind GeoLite2 (mmap в RAM, 0 внешних вызовов)
    geolite2_asn_path: str = "/data/geoip/GeoLite2-ASN.mmdb"
    geolite2_city_path: str = "/data/geoip/GeoLite2-City.mmdb"

    # Cloudflare Turnstile и защита периметра
    turnstile_enabled: bool = True
    turnstile_secret_key: str = ""
    turnstile_verify_url: str = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
    turnstile_timeout_s: float = 3.0
    origin_secret: str = ""
    cors_allowed_origins: str = "https://netpulse.my,https://www.netpulse.my"

    # Observability & Alerts (Free Tier)
    sentry_dsn: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    alert_cooldown_s: int = 300

    # TTL и лимиты In-Memory State (< 15 MB)
    session_ttl_s: int = 90
    idempotency_ttl_s: int = 90
    probe_token_ttl_s: int = 60
    salt_cache_ttl_s: int = 300
    heatmap_cache_ttl_s: int = 30
    heatmap_min_samples: int = 5
    rate_limit_window_s: int = 600
    rate_limit_fixed_max: int = 15
    rate_limit_mobile_max: int = 120

    # Scheduler & Monitoring
    enable_scheduler: bool = True
    seed_districts_on_startup: bool = True
    healthcheck_interval_s: float = 5.0
    healthcheck_timeout_s: float = 2.0
    healthcheck_fail_threshold: int = 3
    clock_skew_alert_s: float = 5.0
    outage_detector_interval_s: float = 120.0
    peering_alert_window_s: int = 300
    peering_alert_threshold: int = 30
    retention_days: int = 180
    retention_batch_size: int = 50000
    geoip_reload_interval_s: float = 43200.0  # 12h check/reload .mmdb

    @field_validator("node_secrets_json")
    @classmethod
    def _validate_node_secrets_json(cls, value: str) -> str:
        parsed = json.loads(value)
        if not isinstance(parsed, dict) or not parsed:
            raise ValueError("NODE_SECRETS_JSON must be a non-empty JSON object")
        for node_key, raw_secret in parsed.items():
            int(node_key)
            if not isinstance(raw_secret, str) or not proto.parse_secrets(raw_secret):
                raise ValueError(f"Secret for node {node_key} must be a non-empty string")
        return value

    def parsed_node_secrets(self) -> dict[int, list[str]]:
        raw = json.loads(self.node_secrets_json)
        return {int(node_id): proto.parse_secrets(secret) for node_id, secret in raw.items()}

    def parsed_healthcheck_urls(self) -> dict[int, str]:
        if not self.probe_healthcheck_urls_json.strip():
            return {}
        raw = json.loads(self.probe_healthcheck_urls_json)
        return {int(node_id): str(url).rstrip("/") for node_id, url in raw.items()}

    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()