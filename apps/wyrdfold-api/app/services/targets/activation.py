"""Activation lifecycle: failure reasons and the stalled-row sweep (#557 §3, #649).

``targets.activation_status`` is a linear lifecycle, not a set of
interchangeable terminal states::

    deriving  -> profile derivation in flight
    idle      -> derived, AWAITING activation (the frontend fires
                 POST /targets/{id}/activate on seeing this)
    polling   -> activation pipeline running (poll + retro-score)
    ready     -> activation complete
    error     -> the last attempt failed; see ``activation_error``

Two problems this module fixes:

* **``error`` was silent.** Both writers set the same bare string, so a target
  that failed because the user has no experience profile yet was
  indistinguishable from one that hit a transient LLM/DB blip — and neither the
  UI nor an operator could tell which (#649). Failures now carry a stable
  reason code plus a timestamp, and a successful activation clears them, which
  is what turns re-activation into a genuine retry path.
* **Nothing swept the in-flight states.** ``deriving`` / ``polling`` mean
  "something is working on this". When the detached task dies, nothing notices:
  prod had a target stranded in ``polling`` for 27 days. :func:`sweep_stalled_activations`
  converges those back to ``idle`` so they re-activate on the user's next visit.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, cast

from supabase import AsyncClient

logger = logging.getLogger(__name__)

TARGETS_TABLE = "targets"

#: In-flight states — something is supposed to be actively working on the row.
#: These are what the sweep reclaims; ``idle`` / ``ready`` / ``error`` are all
#: resting states that nothing is expected to advance on its own.
IN_FLIGHT_STATUSES: tuple[str, ...] = ("deriving", "polling")


class ActivationError(StrEnum):
    """Stable reason codes for a failed activation.

    Stored in ``targets.activation_error``. Codes rather than prose so the UI
    can branch on them and phrase the message itself — the distinction that
    matters is whether the user can DO anything about it.
    """

    #: The user has no OptimizedDoc yet, so there is nothing to derive against.
    #: USER-ACTIONABLE: they need to add their experience first.
    NO_EXPERIENCE_PROFILE = "no_experience_profile"
    #: Profile derivation exceeded its timeout. Transient — retry may work.
    DERIVE_TIMEOUT = "derive_timeout"
    #: Anything else the pipeline raised (LLM 402, DB blip, network). Transient.
    PIPELINE_FAILED = "pipeline_failed"
    #: The activation never finished and the sweep reclaimed the row. Written
    #: with ``activation_status = 'idle'``, NOT ``'error'``, so the user still
    #: sees a re-activatable target rather than a red card (see the sweep's
    #: docstring). It exists purely so reclaims leave a durable trace: before
    #: this, a reclaim logged a warning that aged out and changed nothing on
    #: the row, so how often deferred work is abandoned was unknowable (#1090).
    STALLED_RECLAIMED = "stalled_reclaimed"


#: Reasons the user can resolve themselves. Everything else is a backend
#: condition where the honest advice is "try again".
USER_ACTIONABLE_ERRORS: frozenset[str] = frozenset({ActivationError.NO_EXPERIENCE_PROFILE})


def is_user_actionable(reason: str | None) -> bool:
    """True when the user can fix the cause themselves (vs. retry-and-hope)."""
    return reason in USER_ACTIONABLE_ERRORS


async def sweep_stalled_activations(
    supabase: AsyncClient, *, stale_after_hours: int
) -> dict[str, int]:
    """Converge targets stuck in an in-flight state back to ``idle``.

    Returns ``{status: rows_reclaimed}`` for the log.

    Why ``idle`` and not ``error``: ``idle`` is the re-activatable state. The
    frontend fires ``/activate`` on seeing it and ``_activate_pipeline``
    re-derives when the profile is missing, so a reclaimed row heals itself on
    the user's next visit rather than showing them a red card for a stall they
    did not cause.

    ``stale_after_hours`` is deliberately generous (hours, not minutes): the
    cutoff keys on ``updated_at``, which a long-running pipeline does NOT touch
    between status transitions, so too tight a window could reclaim a live
    activation. That failure mode is benign — the row re-activates, duplicating
    work but losing nothing — but the point of the sweep is stalls, not races.
    Idempotent: a second run inside the same window matches nothing new.
    """
    cutoff = (datetime.now(UTC) - timedelta(hours=stale_after_hours)).isoformat()
    reclaimed: dict[str, int] = {}
    for status in IN_FLIGHT_STATUSES:
        resp = await (
            supabase.table(TARGETS_TABLE)
            .update(
                {
                    "activation_status": "idle",
                    # Durable trace of the reclaim (#1090). The status stays
                    # ``idle`` so the card is unchanged for the user — the
                    # frontend gates its failure message on the status, not on
                    # this column — but the row now says it was abandoned, and
                    # by when. Counting these is how the frequency of lost
                    # deferred work gets sized instead of guessed.
                    #
                    # Known undercount: re-activating a target clears both
                    # columns, so this counts targets reclaimed and NOT since
                    # recovered. That is the population that matters most, but
                    # it is not the total number of reclaims.
                    "activation_error": ActivationError.STALLED_RECLAIMED,
                    "activation_failed_at": datetime.now(UTC).isoformat(),
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )
            .eq("activation_status", status)
            .lt("updated_at", cutoff)
            .execute()
        )
        rows = cast(list[dict[str, Any]], resp.data or [])
        reclaimed[status] = len(rows)
        if rows:
            logger.warning(
                "activation sweep: reclaimed %d target(s) stalled in %r for >%dh -> idle (%s)",
                len(rows),
                status,
                stale_after_hours,
                ", ".join(str(r.get("id")) for r in rows),
            )
    return reclaimed
