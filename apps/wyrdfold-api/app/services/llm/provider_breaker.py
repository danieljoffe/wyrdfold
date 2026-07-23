"""Shared provider-fatal fast-fail breaker (audit PERF-M "402/429 fast-fail", #422).

Latched (in-process, monotonic) when the LLM *provider* rejects a call for a
reason that will reject every other call too — OpenRouter out of credits (402)
or sustained rate-limiting (429 after the client's own retries) — which can
happen while we're still under our self-imposed spend cap. Callers check
:func:`provider_fatal_active` before an LLM round-trip and skip it while latched,
and call :func:`trip_provider_fatal` on catching the fatal error; the latch
auto-clears after the cooldown so the next attempt retries once (credits may be
topped up / the 429 window may have passed).

Shared by the poller's qualify fan-out AND the E2 lazy fit-score refresh, so a
provider outage detected by either backs the other off too — in particular a
credits outage can't make the refresh churn the DB by re-scheduling doomed LLM
calls on every ``/targets/mine`` view. In-process (per worker) is sufficient:
each worker latches on its first fatal error and stops hammering.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

PROVIDER_FATAL_COOLDOWN_S = 300.0
_provider_fatal_until = 0.0


def trip_provider_fatal(exc: BaseException) -> None:
    """Latch the fast-fail breaker for the cooldown after a provider 402/429."""
    global _provider_fatal_until
    _provider_fatal_until = time.monotonic() + PROVIDER_FATAL_COOLDOWN_S
    logger.warning(
        "LLM provider fast-fail latched for %.0fs — provider rejecting calls "
        "(%s: %s). Deferring dependent LLM work; it retries after the cooldown. "
        "If this is a 402, top up OpenRouter credits.",
        PROVIDER_FATAL_COOLDOWN_S,
        type(exc).__name__,
        exc,
    )


def provider_fatal_active() -> bool:
    """True while the provider-fatal breaker is latched (within the cooldown)."""
    return time.monotonic() < _provider_fatal_until


def reset_for_tests() -> None:
    """Clear the latch. Test-only — the latch is process-global, so a test that
    trips it must reset it so it doesn't leak into later tests."""
    global _provider_fatal_until
    _provider_fatal_until = 0.0
