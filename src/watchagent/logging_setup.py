"""Structured-ish logging setup.

Keeps everything on ``logging.getLogger("watchagent")`` and applies a single
formatter that includes the level, logger name, and any ``extra=`` fields the
caller passes. This is what the ``logging-contract.mdc`` rule references.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any


class _StructuredFormatter(logging.Formatter):
    """Render log records as ``LEVEL name event key=value key=value``."""

    _RESERVED = {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "message",
    }

    def format(self, record: logging.LogRecord) -> str:
        base = f"{record.levelname:<7} {record.name} {record.getMessage()}"
        extras: dict[str, Any] = {
            k: v for k, v in record.__dict__.items() if k not in self._RESERVED
        }
        if extras:
            tail = " ".join(f"{k}={_render(v)}" for k, v in sorted(extras.items()))
            base = f"{base} {tail}"
        if record.exc_info:
            base = f"{base}\n{self.formatException(record.exc_info)}"
        return base


def _render(value: Any) -> str:
    if isinstance(value, str) and " " not in value:
        return value
    return json.dumps(value, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Idempotently configure the root logger."""
    root = logging.getLogger()
    root.setLevel(level.upper())
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_StructuredFormatter())
    root.addHandler(handler)
