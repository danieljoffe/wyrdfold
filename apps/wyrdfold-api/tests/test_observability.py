"""Sentry PII scrubbing (#29 P4).

Pins the ``before_send`` contract: sensitive values are redacted by key
name across nested context and exception-frame locals; benign diagnostic
fields survive; the hook always returns an event (never drops or raises).
"""

from __future__ import annotations

from app.observability import _is_sensitive, _scrub_event


def test_is_sensitive_matches_secrets_and_pii() -> None:
    for key in (
        "password",
        "X-Api-Key",
        "OPENROUTER_API_KEY",
        "Authorization",
        "session_cookie",
        "user_email",
        "phone_e164",
        "resume_text",
        "byok_master_key",
        "ciphertext",
    ):
        assert _is_sensitive(key), key


def test_is_sensitive_leaves_benign_keys() -> None:
    for key in ("status", "title", "count", "target_id", "method", "url"):
        assert not _is_sensitive(key), key


def test_scrubs_top_level_context_and_headers() -> None:
    event = {
        "extra": {
            "openrouter_api_key": "or-secret",
            "user_email": "jane@example.com",
            "job_count": 7,
        },
        "request": {
            "headers": {"Authorization": "Bearer abc", "X-Request-Id": "r1"},
            "cookies": {"sb-access-token": "tok"},
        },
    }
    out = _scrub_event(event)
    assert out is not None
    assert out["extra"]["openrouter_api_key"] == "[Filtered]"
    assert out["extra"]["user_email"] == "[Filtered]"
    assert out["extra"]["job_count"] == 7
    assert out["request"]["headers"]["Authorization"] == "[Filtered]"
    assert out["request"]["headers"]["X-Request-Id"] == "r1"
    # The whole `cookies` value is redacted (key contains "cookie").
    assert out["request"]["cookies"] == "[Filtered]"


def test_scrubs_exception_frame_locals() -> None:
    event = {
        "exception": {
            "values": [
                {
                    "stacktrace": {
                        "frames": [{"vars": {"resume_text": "Jane Doe — Staff Eng", "n": 1}}]
                    }
                }
            ]
        }
    }
    out = _scrub_event(event)
    assert out is not None
    frame = out["exception"]["values"][0]["stacktrace"]["frames"][0]
    assert frame["vars"]["resume_text"] == "[Filtered]"
    assert frame["vars"]["n"] == 1


def test_returns_event_unchanged_when_nothing_sensitive() -> None:
    event = {"level": "error", "extra": {"a": {"b": "c"}}}
    assert _scrub_event(event) == event


def test_never_raises_and_always_returns_an_event() -> None:
    # A deeply nested structure beyond the depth guard still returns cleanly.
    nested: dict = {"extra": {}}
    cur = nested["extra"]
    for _ in range(30):
        cur["next"] = {}
        cur = cur["next"]
    out = _scrub_event(nested)
    assert isinstance(out, dict)
