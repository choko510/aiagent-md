"""Structured JSON logging with secret redaction.

Log records carry codes and identifiers, never message bodies, tool payloads, prompts or credentials. As a second
line of defence, values that look like credentials are masked before output.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from typing import Any

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)(api[_-]?key|password|secret|token|cookie)(\"?\s*[:=]\s*\"?)[^\s\",}]{4,}"),
    re.compile(r"postgres(?:ql)?://[^:@/\s]+:[^@/\s]+@"),
]
_registered: set[str] = set()


def register_secret(value: str) -> None:
    """Exact secret values loaded by this process are always masked."""
    if value and len(value) >= 6:
        _registered.add(value)


def redact(text: str) -> str:
    for s in _registered:
        text = text.replace(s, "***")
    for p in _SECRET_PATTERNS:
        text = p.sub(lambda m: (m.group(1) + m.group(2) + "***") if m.lastindex and m.lastindex >= 2 else "***", text)
    return text


class JsonFormatter(logging.Formatter):
    def __init__(self, component: str) -> None:
        super().__init__()
        self.component = component

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "component": self.component,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            out.update(fields)
        if record.exc_info and record.exc_info[0] is not None:
            out["exc_type"] = record.exc_info[0].__name__
        return redact(json.dumps(out, ensure_ascii=False, default=str))


def setup(component: str, level: str = "INFO") -> logging.Logger:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(JsonFormatter(component))
    root.addHandler(h)
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "psycopg.pool"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger(component)


def log(logger: logging.Logger, level: int, msg: str, **fields: Any) -> None:
    logger.log(level, msg, extra={"fields": fields})
