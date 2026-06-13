"""Structured (JSON) logging opt-in (#26 F5).

When ``WYRDFOLD_LOG_FORMAT=json``, attach a JSON formatter to the root
logger so application log lines are machine-parseable. Default stays
text so local dev keeps the readable output.

Scope: covers loggers that propagate to root — every ``logging.getLogger(__name__)``
caller in ``app/`` does. Uvicorn's own loggers (``uvicorn``,
``uvicorn.access``, ``uvicorn.error``) keep their stock formatting; we
deliberately don't touch them here to avoid coupling boot-time logging
config to uvicorn internals. Operators wanting fully-unified JSON logs
can pass ``--log-config /path/to/log.json`` to uvicorn (see the README
Operations section).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

# Standard ``LogRecord`` attributes the formatter shouldn't echo as
# user-extras — these either get rendered by the formatter directly
# (``levelname``, ``msg``…) or are stdlib bookkeeping (``args``, ``pathname``).
# Anything left over after this filter is an ``extra=`` field the caller
# attached on purpose.
_RESERVED = frozenset(
    {
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
        "message",
        "module",
        "msecs",
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
)


class JsonFormatter(logging.Formatter):
    """Emit each log record as a single-line JSON object.

    Shape is intentionally flat:

    ::

        {"ts": "2026-06-13T03:00:00.123Z",
         "level": "WARNING",
         "logger": "app.services.poller",
         "message": "slow_request method=GET path=/jobs duration_ms=812.4",
         "extra_field": "..."}

    Exception tracebacks land in a top-level ``exc_info`` string so
    log-aggregation tools that index full text still match on them.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Surface caller-supplied ``extra={...}`` fields. Anything in
        # ``__dict__`` that isn't a reserved LogRecord attribute is an
        # extra; serialize as-is when JSON-encodable, repr otherwise.
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


def init_logging(log_format: str) -> None:
    """Wire the JSON formatter onto the root logger when opted in.

    Idempotent: re-init replaces any prior JsonFormatter handler so a
    reload (e.g. ``uvicorn --reload``) doesn't stack duplicates.

    No-op when ``log_format`` is anything other than ``"json"`` — falls
    through to uvicorn / stdlib defaults so local dev stays readable.
    """
    if log_format != "json":
        return

    root = logging.getLogger()
    # Clear any prior JSON handler from a reload — keep other handlers
    # the host may have attached (uvicorn, Sentry, etc.).
    for h in list(root.handlers):
        if isinstance(h.formatter, JsonFormatter):
            root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    # Don't lower the level beyond what callers configured — but make
    # sure root has a level set so child loggers without explicit
    # configuration aren't silenced by the WARNING default.
    if root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
