import logging
import sys
from typing import Any

import sentry_sdk
import structlog
from sentry_sdk.integrations.fastapi import FastApiIntegration

PII_FIELD_NAMES = frozenset(
    {
        "ip",
        "raw_ip",
        "client_ip",
        "remote_addr",
        "cf_connecting_ip",
        "cf-connecting-ip",
        "x_forwarded_for",
        "x-forwarded-for",
        "x-real-ip",
        "lat",
        "lon",
        "latitude",
        "longitude",
        "gps",
        "client_geo",
        "authorization",
        "cookie",
        "x-origin-secret",
        "x_origin_secret",
        "x-turnstile-token",
        "turnstile_token",
    }
)


def _scrub_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: "[REDACTED]" if str(k).lower() in PII_FIELD_NAMES else _scrub_value(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub_value(item) for item in value]
    return value


def strip_pii_fields(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Гарантирует Zero-PII в JSON-логах (PDPA Compliance)."""
    cleaned: dict[str, Any] = {}
    for key, value in event_dict.items():
        if key.lower() in PII_FIELD_NAMES:
            continue
        cleaned[key] = _scrub_value(value)
    return cleaned


def configure_logging(environment: str = "production") -> None:
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.INFO,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            strip_pii_fields,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    structlog.get_logger("central").info("logging_configured", environment=environment)


def _sentry_before_send(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any]:
    request = event.get("request")
    if isinstance(request, dict):
        if isinstance(request.get("headers"), dict):
            request["headers"] = _scrub_value(request["headers"])
        if isinstance(request.get("data"), dict):
            request["data"] = _scrub_value(request["data"])
        request.pop("env", None)
    event.pop("user", None)
    return event


def configure_sentry(dsn: str, environment: str) -> None:
    if not dsn:
        return
    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        send_default_pii=False,
        before_send=_sentry_before_send,
        traces_sample_rate=0.05,
        integrations=[FastApiIntegration()],
    )