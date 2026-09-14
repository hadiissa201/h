"""Structured logging.

Two guarantees:

* every log line is a single JSON object (when ``LOG_FORMAT=json``) so n8n /
  Loki / ``jq`` can consume it without regexes;
* known-secret keys are redacted before serialisation, so an accidental
  ``log_event(..., api_key=...)`` cannot leak a credential into stdout.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pythonjsonlogger.json import JsonFormatter

from app.core.events import EventType

_SECRET_MARKERS = (
    "secret",
    "password",
    "passwd",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "private_key",
)
REDACTED = "***REDACTED***"

_RESERVED = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "message",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
    "taskName",
}


def redact(payload: Any) -> Any:
    """Recursively replace values whose key looks like a secret."""
    if isinstance(payload, dict):
        clean: dict[str, Any] = {}
        for key, value in payload.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in _SECRET_MARKERS):
                clean[str(key)] = REDACTED if value not in (None, "") else value
            else:
                clean[str(key)] = redact(value)
        return clean
    if isinstance(payload, list | tuple):
        return [redact(item) for item in payload]
    if isinstance(payload, Decimal):
        return float(payload)
    if isinstance(payload, datetime):
        return payload.isoformat()
    return payload


class _TradingFormatter(JsonFormatter):
    def add_fields(
        self,
        log_record: dict[str, Any],
        record: logging.LogRecord,
        message_dict: dict[str, Any],
    ) -> None:
        super().add_fields(log_record, record, message_dict)
        log_record["timestamp"] = datetime.fromtimestamp(
            record.created, tz=UTC
        ).isoformat()
        log_record["level"] = record.levelname
        log_record["logger"] = record.name
        extras = {
            key: value for key, value in record.__dict__.items() if key not in _RESERVED
        }
        for key, value in redact(extras).items():
            log_record.setdefault(key, value)
        log_record.pop("color_message", None)


class _ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED and not key.startswith("_")
        }
        if extras:
            rendered = " ".join(f"{k}={v}" for k, v in redact(extras).items())
            return f"{base} | {rendered}"
        return base


_configured = False


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    global _configured
    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(_TradingFormatter("%(message)s"))
    else:
        handler.setFormatter(
            _ConsoleFormatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
        )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # uvicorn duplicates access logs through its own handlers
    for noisy in ("uvicorn.access", "uvicorn.error"):
        logging.getLogger(noisy).propagate = True
        logging.getLogger(noisy).handlers = []
    _configured = True


def get_logger(name: str) -> logging.Logger:
    if not _configured:
        configure_logging()
    return logging.getLogger(name)


def log_event(
    logger: logging.Logger,
    event: EventType | str,
    *,
    level: int = logging.INFO,
    message: str | None = None,
    **fields: Any,
) -> None:
    """Emit one structured event line.

    ``fields`` are merged into the JSON record after redaction. Conventional
    keys: ``symbol``, ``timeframe``, ``strategy``, ``decision``, ``confidence``,
    ``price``, ``risk``, ``reason``, ``trade_id``, ``order_id``, ``position_id``.
    """
    payload = redact(dict(fields))
    payload["event"] = str(event)
    logger.log(level, message or str(event), extra=payload)
