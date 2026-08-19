"""JSON logging opt-in (#26 F5)."""

from __future__ import annotations

import io
import json
import logging
import sys

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


def test_init_logging_text_leaves_handlers_alone(caplog: pytest.LogCaptureFixture) -> None:
    """Default `text` must not touch root HANDLERS.

    It does set the root LEVEL — see the level tests below. Formatting and
    verbosity are deliberately separate concerns (#862).
    """
    root = logging.getLogger()
    before = list(root.handlers)
    init_logging("text")
    assert root.handlers == before


def test_init_logging_json_attaches_handler() -> None:
    """`json` adds one StreamHandler with a JsonFormatter to root."""
    root = logging.getLogger()
    before_count = sum(1 for h in root.handlers if isinstance(h.formatter, JsonFormatter))

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
        json_handlers = [h for h in root.handlers if isinstance(h.formatter, JsonFormatter)]
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


# ---- level is independent of format (#862) ----------------------------------
#
# These exist because the two were entangled: `setLevel` lived inside the
# json-only branch, so production (LOG_FORMAT unset -> "text") silently
# discarded every application logger.info. uvicorn's own access logger kept
# emitting at INFO, which made the gap look like ordinary traffic.


@pytest.fixture
def restore_root_level():
    root = logging.getLogger()
    before_level = root.level
    before_handlers = list(root.handlers)
    yield
    root.setLevel(before_level)
    root.handlers[:] = before_handlers


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_app_loggers_emit_info_whatever_the_format(fmt: str, restore_root_level: None) -> None:
    """The property that actually matters: after init, an application logger
    is enabled for INFO. Asserted for BOTH formats — the bug was that one of
    them silently was not."""
    app_logger = logging.getLogger("app.routers.billing")

    # Precondition, so this cannot pass vacuously: at the stdlib default the
    # logger is NOT enabled for INFO. That default is exactly what production
    # was running on.
    logging.getLogger().setLevel(logging.WARNING)
    assert not app_logger.isEnabledFor(logging.INFO)

    init_logging(fmt)

    assert app_logger.isEnabledFor(logging.INFO), (
        f"LOG_FORMAT={fmt!r} must not decide whether INFO records survive"
    )


def test_explicit_level_is_honoured(restore_root_level: None) -> None:
    init_logging("text", "WARNING")
    assert not logging.getLogger("app.routers.billing").isEnabledFor(logging.INFO)
    assert logging.getLogger("app.routers.billing").isEnabledFor(logging.WARNING)

    init_logging("text", "debug")  # case-insensitive
    assert logging.getLogger("app.routers.billing").isEnabledFor(logging.DEBUG)


def test_unknown_level_falls_back_to_info_not_silence(restore_root_level: None) -> None:
    """A typo in LOG_LEVEL must not silence the application. Failing loud is
    the safe direction for an observability setting."""
    logging.getLogger().setLevel(logging.WARNING)
    init_logging("text", "verbose-please")
    assert logging.getLogger("app.routers.billing").isEnabledFor(logging.INFO)


# ---- the level is NOT sufficient — records must actually be EMITTED --------
#
# The first pass at #862 asserted `isEnabledFor(INFO)` and shipped green while
# production still dropped every INFO line. `isEnabledFor` is necessary and NOT
# sufficient: uvicorn attaches handlers to its own loggers and never to root, so
# a record can pass the level check, propagate to a handler-less root, and fall
# through to `logging.lastResort` — which emits at WARNING and above only.
#
# These assert the property that actually matters: the text comes out.


def _uvicorn_style_root() -> None:
    """Root as uvicorn leaves it: WARNING, and NO handlers of its own."""
    root = logging.getLogger()
    root.handlers[:] = []
    root.setLevel(logging.WARNING)


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_info_records_are_actually_emitted(
    fmt: str, capsys: pytest.CaptureFixture[str], restore_root_level: None
) -> None:
    _uvicorn_style_root()
    app_logger = logging.getLogger("app.scheduler")

    # Precondition: exactly the state that silently dropped INFO in production.
    app_logger.info("before-init-should-not-appear")
    assert "before-init-should-not-appear" not in capsys.readouterr().out

    init_logging(fmt)
    app_logger.info("after-init-sentinel")

    assert "after-init-sentinel" in capsys.readouterr().out, (
        f"LOG_FORMAT={fmt!r}: an INFO record must reach a handler, not merely pass the level check"
    )


def test_existing_root_handlers_are_not_duplicated(
    capsys: pytest.CaptureFixture[str], restore_root_level: None
) -> None:
    """A host that configured its own handler keeps it, and records appear once."""
    _uvicorn_style_root()
    root = logging.getLogger()
    root.addHandler(logging.StreamHandler(sys.stdout))

    init_logging("text")
    logging.getLogger("app.scheduler").warning("once-only-sentinel")

    assert capsys.readouterr().out.count("once-only-sentinel") == 1
