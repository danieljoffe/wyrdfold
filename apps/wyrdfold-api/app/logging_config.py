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
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat().replace("+00:00", "Z"),
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


def init_logging(log_format: str, log_level: str = "INFO") -> None:
    """Set the root log LEVEL, and wire the JSON formatter when opted in.

    Level and format are independent concerns and must not share a branch
    (#862). They used to: the ``setLevel`` call lived inside the
    ``json``-only body, so the default ``text`` format left root at the
    stdlib ``WARNING`` and production silently discarded **every**
    application ``logger.info`` — 84 call sites, including billing plan
    changes and the whole of ``scheduler.py``'s outcome reporting. uvicorn
    configures its own ``uvicorn.access`` logger at INFO, so request lines
    kept appearing and the gap looked like normal traffic.

    The old guard was ``if root.level == logging.NOTSET``, which can never
    fire: root's level is ``WARNING`` (30) both on a fresh interpreter and
    after uvicorn's ``dictConfig``. So the call was dead code, and setting
    ``LOG_FORMAT=json`` would NOT have fixed it either — the level is now
    set unconditionally from ``log_level``.

    Idempotent: re-init replaces any prior JsonFormatter handler so a
    reload (e.g. ``uvicorn --reload``) doesn't stack duplicates.

    Formatting still falls through to uvicorn / stdlib defaults for any
    ``log_format`` other than ``"json"``, so local dev stays readable.
    """
    root = logging.getLogger()
    # LEVEL FIRST, and unconditionally — before the format early-return.
    resolved = logging.getLevelName(log_level.upper())
    if not isinstance(resolved, int):  # unknown name -> INFO, never silence
        logging.getLogger(__name__).warning(
            "unknown LOG_LEVEL %r — falling back to INFO", log_level
        )
        resolved = logging.INFO
    root.setLevel(resolved)

    if log_format != "json":
        # LEVEL ALONE IS NOT ENOUGH. uvicorn attaches handlers to its OWN
        # loggers (`uvicorn`, `uvicorn.access`, …) and never to root, so an
        # application record that passes the level check finds no handler and
        # falls through to ``logging.lastResort`` — which emits at WARNING and
        # above only. That is precisely why app WARNING/ERROR lines have always
        # reached Railway while INFO never did, and why raising the level in
        # isolation changed nothing (#862, second pass).
        #
        # Only attach when root has nothing, so a host that configured its own
        # handlers (Sentry, a dictConfig, a re-init under --reload) is left
        # alone and records are not duplicated.
        if not root.handlers:
            plain = logging.StreamHandler(sys.stdout)
            plain.setFormatter(logging.Formatter("%(levelname)s [%(name)s] %(message)s"))
            root.addHandler(plain)
        return

    # Clear any prior JSON handler from a reload — keep other handlers
    # the host may have attached (uvicorn, Sentry, etc.).
    for h in list(root.handlers):
        if isinstance(h.formatter, JsonFormatter):
            root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
