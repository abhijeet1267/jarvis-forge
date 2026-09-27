"""Structured logging for JARVIS Forge.

Console output stays human-readable; the log file receives a compact JSON line
per record so the dashboard and later analytics can parse it.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict

from .config import get_settings

_CONFIGURED = False


class JsonFormatter(logging.Formatter):
    """Render a log record as a single JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Attach any structured extras the caller passed via `extra=`.
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class _ExtraAdapter(logging.LoggerAdapter):
    """Lets callers do ``log.info("msg", extra_fields={...})``."""

    def process(self, msg: str, kwargs: Dict[str, Any]):
        extras = kwargs.pop("extra_fields", None)
        if extras:
            kwargs.setdefault("extra", {})["extra_fields"] = extras
        return msg, kwargs


def setup_logging() -> None:
    """Idempotently configure the root logger."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    settings = get_settings()
    settings.ensure_dirs()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    root = logging.getLogger("jarvis")
    root.setLevel(level)
    root.propagate = False

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    )
    root.addHandler(console)

    file_handler = logging.FileHandler(settings.log_path, encoding="utf-8")
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)

    _CONFIGURED = True


def get_logger(name: str) -> logging.LoggerAdapter:
    """Return a namespaced logger under the ``jarvis`` tree."""
    setup_logging()
    return _ExtraAdapter(logging.getLogger(f"jarvis.{name}"), {})
