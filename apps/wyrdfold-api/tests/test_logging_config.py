"""JSON logging opt-in (#26 F5)."""

from __future__ import annotations

import io
import json
import logging

import pytest

from app.logging_config import JsonFormatter, init_logging


def _record(
    name: str = "app.tests",
    level: int = logging.INFO,
    msg: str = "hello",
    args: tuple = (),
    **extra: object,
) -> logging.LogRecord:
    record = logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=10,
        msg=msg,
        args=args,
        exc_info=None,
    )
    for k, v in extra.items():
        setattr(record, k, v)
    return record


def test_formatter_emits_required_fields_as_single_json_line() -> None:
    fmt = JsonFormatter()
    out = fmt.format(_record(msg="slow_request path=/jobs"))
    assert "\n" not in out
    payload = json.loads(out)
    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.tests"
    assert payload["message"] == "slow_request path=/jobs"
    assert payload["ts"].endswith("Z")  # UTC marker


def test_formatter_surfaces_extras() -> None:
    """`logger.info('...', extra={user_id: 'u-1'})` should round-trip."""
    fmt = JsonFormatter()
    payload = json.loads(fmt.format(_record(user_id="u-1", target_id="t-2")))
    assert payload["user_id"] == "u-1"
    assert payload["target_id"] == "t-2"


def test_formatter_handles_non_json_extra_via_repr() -> None:
    """Non-JSON-encodable extras shouldn't crash the formatter."""

    class Unserializable:
        def __repr__(self) -> str:
            return "<Unserializable>"

    fmt = JsonFormatter()
    payload = json.loads(fmt.format(_record(weird=Unserializable())))
    assert payload["weird"] == "<Unserializable>"


def test_formatter_includes_exception_traceback() -> None:
    fmt = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="app.tests",
            level=logging.ERROR,
            pathname=__file__,
            lineno=20,
            msg="explosion",
            args=(),
            exc_info=sys.exc_info(),
        )
    payload = json.loads(fmt.format(record))
    assert payload["level"] == "ERROR"
    assert "ValueError: boom" in payload["exc_info"]


def test_init_logging_text_is_noop(caplog: pytest.LogCaptureFixture) -> None:
    """Default `text` must not touch root handlers."""
    root = logging.getLogger()
    before = list(root.handlers)
    init_logging("text")
    assert root.handlers == before


def test_init_logging_json_attaches_handler() -> None:
    """`json` adds one StreamHandler with a JsonFormatter to root."""
    root = logging.getLogger()
    before_count = sum(
        1 for h in root.handlers if isinstance(h.formatter, JsonFormatter)
    )

    init_logging("json")

    try:
        after = [h for h in root.handlers if isinstance(h.formatter, JsonFormatter)]
        # Exactly one JSON handler total (previous one removed if any).
        assert len(after) == 1
        assert before_count <= 1
    finally:
        # Clean up so other tests see a stock root logger.
        for h in list(root.handlers):
            if isinstance(h.formatter, JsonFormatter):
                root.removeHandler(h)


def test_init_logging_json_is_idempotent() -> None:
    """Repeat init (e.g. uvicorn --reload) must not stack duplicates."""
    root = logging.getLogger()
    try:
        init_logging("json")
        init_logging("json")
        json_handlers = [
            h for h in root.handlers if isinstance(h.formatter, JsonFormatter)
        ]
        assert len(json_handlers) == 1
    finally:
        for h in list(root.handlers):
            if isinstance(h.formatter, JsonFormatter):
                root.removeHandler(h)


def test_json_handler_writes_to_stdout() -> None:
    """Smoke test: an actual log call in JSON mode emits a valid JSON line."""
    fmt = JsonFormatter()
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(fmt)
    logger = logging.getLogger("app.tests.smoke")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.info("ingested job", extra={"job_id": "j-1"})

    line = buf.getvalue().strip()
    payload = json.loads(line)
    assert payload["message"] == "ingested job"
    assert payload["job_id"] == "j-1"
