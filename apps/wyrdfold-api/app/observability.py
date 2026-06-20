"""Sentry wiring. Must be imported before FastAPI is instantiated.

Sentry's FastAPI integration patches Starlette middleware at init time, so
``init_sentry()`` has to run before ``app = FastAPI(...)``. Callers with no
DSN configured get a no-op (useful for local dev and tests without
credentials).

PII handling (#29 P4): ``send_default_pii=False`` already keeps request
bodies, cookies, and client IPs out of events, and the SDK's built-in
``EventScrubber`` redacts a denylist of secret-ish keys. On top of that we
attach a ``before_send`` hook (``_scrub_event``) that redacts our own
PII/secret key names — résumé/JD text, emails, phone numbers, and the
provider/BYOK key material — wherever they ride along in structured
context or exception-frame locals. Matching is substring and biased toward
over-redaction: privacy beats keeping a stray diagnostic field.
"""

from __future__ import annotations

from typing import Any

import sentry_sdk
from sentry_sdk.types import Event, Hint

from app.config import settings

# Separator-free fragments whose values get redacted wherever they appear
# as a dict key. The key is normalized to lowercase alphanumerics before
# matching, so ``api_key`` / ``api-key`` / ``apikey`` / ``X-Api-Key`` all
# hit the same fragment. Biased toward recall — e.g. "token" also filters
# numeric token-count fields in a captured error; that trade is intentional,
# since these only show up in rare exception payloads.
_SENSITIVE_KEY_FRAGMENTS: tuple[str, ...] = (
    "apikey",
    "secret",
    "password",
    "passwd",
    "token",
    "jwt",
    "bearer",
    "authorization",
    "cookie",
    "credential",
    "ciphertext",
    "privatekey",
    "masterkey",
    "servicerole",
    "resume",
    "prose",
    "coverletter",
    "jobdescription",
    "email",
    "phone",
    "last4",
    "ssn",
)
_REDACTED = "[Filtered]"
_MAX_DEPTH = 12


def _is_sensitive(key: str) -> bool:
    normalized = "".join(ch for ch in key.lower() if ch.isalnum())
    return any(frag in normalized for frag in _SENSITIVE_KEY_FRAGMENTS)


def _scrub(value: Any, depth: int = 0) -> Any:
    """Recursively redact values whose key looks sensitive.

    Returns a new structure (dicts/lists rebuilt); scalars pass through.
    Depth-guarded so a cyclic/huge event can't blow the stack.
    """
    if depth > _MAX_DEPTH:
        return value
    if isinstance(value, dict):
        return {
            k: (_REDACTED if isinstance(k, str) and _is_sensitive(k) else _scrub(v, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub(v, depth + 1) for v in value]
    if isinstance(value, tuple):
        return tuple(_scrub(v, depth + 1) for v in value)
    return value


def _scrub_event(event: Event, _hint: Hint | None = None) -> Event | None:
    """Sentry ``before_send`` hook — redact sensitive values by key name.

    Returns the scrubbed event so it is still delivered (returning ``None``
    would drop it). Never raises: a scrubbing bug must not suppress error
    reporting, so on failure we send the event unchanged.
    """
    try:
        scrubbed: Event = _scrub(event)
        return scrubbed
    except Exception:
        return event


def init_sentry() -> None:
    if not settings.sentry_dsn:
        return

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.sentry_environment,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        send_default_pii=False,
        before_send=_scrub_event,
        before_send_transaction=_scrub_event,
    )
