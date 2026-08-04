"""Per-(user, target) feedback CRUD + deterministic learner (v1).

v1 learner is intentionally LLM-free: when N>=3 unapplied
``irrelevant`` signals share a literal token in their ``reason`` field,
that token gets appended to the target's
``scoring_profile.negative.keywords`` and the consumed rows get stamped
with ``applied_at``. v2 will layer an LLM ``ProfilePatch`` on top of
this same plumbing.

Why deterministic first: an LLM patch is expensive and only useful once
we have labeled data to validate against. The literal-token path moves
the needle for noisy targets at zero LLM cost and produces a clean
audit trail before v2 ships.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections import Counter
from datetime import UTC, datetime
from typing import Any, cast

from supabase import AsyncClient, Client

from app.models.feedback import (
    FeedbackRow,
    FeedbackSignal,
    LearnerPatchSummary,
)
from app.services.targets.profile_writes import apply_profile_patch_rpc

logger = logging.getLogger(__name__)

TABLE = "job_feedback"

# Minimum unapplied "irrelevant" signals before the learner does anything.
# Picked to balance signal vs noise: 1-2 clicks could be a misclick, 3+
# is a pattern. Tuned alongside ``_MIN_TOKEN_FREQUENCY`` below.
_MIN_FEEDBACK_FOR_LEARN = 3

# A token must appear in >= this many distinct unapplied irrelevant rows
# to be auto-added as a negative keyword. Same number as the trip
# threshold above so a single 3-click batch with one shared token
# applies — but a single click with a long shared phrase doesn't.
_MIN_TOKEN_FREQUENCY = 3

# Words we don't want to learn as negatives even if they show up
# repeatedly — they're either generic or already structural.
_LEARN_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "the",
        "or",
        "but",
        "if",
        "of",
        "to",
        "for",
        "in",
        "on",
        "at",
        "by",
        "with",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "i",
        "me",
        "my",
        "we",
        "us",
        "our",
        "you",
        "your",
        "he",
        "she",
        "they",
        "them",
        "not",
        "no",
        "too",
        "very",
        "so",
        "than",
        "then",
        "just",
        "really",
        # Domain-generic words that would zero-out half the corpus if learned.
        "job",
        "role",
        "position",
        "company",
        "team",
        "work",
        "opportunity",
    }
)

_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z\-]{2,}")


def _parse_row(row: dict[str, Any]) -> FeedbackRow:
    return FeedbackRow.model_validate(row)


# `async def` on the async user client (#57 slice 4): the interactive
# job_feedback CRUD runs natively on the event loop, off the threadpool. The
# deterministic learner below stays sync — it leans on the shared #191
# profile-write RPC + ``bulk_score_for_target`` (service-role, poller-shared),
# so an async fork would twin those; the async handlers drive it via
# ``asyncio.to_thread`` on the pooled sync client instead.
async def upsert_feedback(
    supabase: AsyncClient,
    *,
    user_id: str,
    job_posting_id: str,
    target_id: str,
    signal: FeedbackSignal,
    reason: str | None,
) -> FeedbackRow:
    """Upsert a feedback row. Latest signal wins on the unique key."""
    payload: dict[str, Any] = {
        "user_id": user_id,
        "job_posting_id": job_posting_id,
        "target_id": target_id,
        "signal": signal,
        "reason": reason,
        # An update on (user, job, target) must reset the applied state —
        # otherwise an irrelevant→relevant flip would silently leave the
        # old "consumed" stamp on the row and the new signal would never
        # be processed.
        "applied_at": None,
        "applied_run_id": None,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    resp = (
        await supabase.table(TABLE)
        .upsert(payload, on_conflict="user_id,job_posting_id,target_id")
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        raise RuntimeError("Failed to upsert job_feedback row")
    return _parse_row(rows[0])


async def delete_feedback(
    supabase: AsyncClient, *, user_id: str, job_posting_id: str, target_id: str
) -> bool:
    """Remove a feedback row. Returns True if a row was deleted."""
    resp = (
        await supabase.table(TABLE)
        .delete()
        .eq("user_id", user_id)
        .eq("job_posting_id", job_posting_id)
        .eq("target_id", target_id)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return bool(rows)


async def list_for_target(
    supabase: AsyncClient,
    *,
    user_id: str,
    target_id: str,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[FeedbackRow], int]:
    """Return the user's feedback rows for one target, newest first."""
    from postgrest import CountMethod

    resp = (
        await supabase.table(TABLE)
        .select("*", count=CountMethod.exact)
        .eq("user_id", user_id)
        .eq("target_id", target_id)
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    total = getattr(resp, "count", None) or len(rows)
    return [_parse_row(r) for r in rows], total


# ---- Deterministic learner ------------------------------------------------


def _extract_tokens(reason: str | None) -> list[str]:
    """Lowercase content tokens from a feedback reason, stopwords removed."""
    if not reason:
        return []
    return [
        m.group(0).lower()
        for m in _TOKEN_RE.finditer(reason)
        if m.group(0).lower() not in _LEARN_STOPWORDS
    ]


def _frequent_tokens(rows: list[FeedbackRow], threshold: int) -> list[str]:
    """Tokens that appear in ``>= threshold`` distinct rows. Order: most
    frequent first, ties broken by first-seen order so the result is
    stable across runs."""
    seen_in: Counter[str] = Counter()
    for row in rows:
        for token in set(_extract_tokens(row.reason)):
            seen_in[token] += 1
    return [tok for tok, n in seen_in.most_common() if n >= threshold]


def maybe_run_learner(
    supabase: Client, *, user_id: str, target_id: str
) -> LearnerPatchSummary | None:
    """Run the deterministic learner for one (user, target) pair.

    Returns a summary when something was applied, None when the trip
    threshold wasn't reached (so the caller can keep the API response
    cheap and skip the diff render).
    """
    pending_resp = (
        supabase.table(TABLE)
        .select("*")
        .eq("user_id", user_id)
        .eq("target_id", target_id)
        .eq("signal", "irrelevant")
        .is_("applied_at", "null")
        .order("created_at", desc=True)
        .limit(50)
        .execute()
    )
    pending = [_parse_row(r) for r in cast(list[dict[str, Any]], pending_resp.data or [])]
    if len(pending) < _MIN_FEEDBACK_FOR_LEARN:
        return None

    new_tokens = _frequent_tokens(pending, _MIN_TOKEN_FREQUENCY)
    if not new_tokens:
        # 3+ signals but no token appears in 3+ rows — nothing literal to
        # learn from. v2's LLM step is what handles this case.
        return None

    target_resp = supabase.table("targets").select("*").eq("id", target_id).single().execute()
    target_row = cast(dict[str, Any] | None, target_resp.data)
    if target_row is None:
        return None
    profile = cast(dict[str, Any], target_row.get("scoring_profile") or {})
    negative = cast(dict[str, Any], profile.get("negative") or {})
    existing = {kw.lower() for kw in cast(list[str], negative.get("keywords") or [])}
    truly_new = [t for t in new_tokens if t not in existing]
    if not truly_new:
        # All frequent tokens are already in the negative list — nothing
        # to do. Don't bump version, don't stamp rows.
        return None

    negative["keywords"] = cast(list[str], negative.get("keywords") or []) + truly_new
    if "weight" not in negative:
        # Mirror the default the LLM derivation emits so the merged profile
        # remains a valid ``NegativeProfile``.
        negative["weight"] = -10.0
    profile["negative"] = negative

    # Write the shared profile through the #191 version-checked / follower-
    # re-checked patch RPC — the same path the LLM learner and reference-JD
    # merges use — instead of a raw read-modify-write ``.update()``. That closed
    # the last un-migrated direct write to a shared ``scoring_profile``: a
    # Python authz slip can't poison a profile every follower shares, and two
    # concurrent learner runs (or a learner racing an LLM-learner / ref-JD
    # merge) can't lose an update (hardening review 2026-07-21, SEC-2).
    expected_version = int(target_row.get("profile_version") or 1)
    outcome, new_version = apply_profile_patch_rpc(
        supabase,
        user_id=user_id,
        target_id=target_id,
        next_profile=profile,
        expected_version=expected_version,
    )
    if outcome != "applied" or new_version is None:
        # Concurrent writer bumped the version (version_conflict) or the in-DB
        # membership re-check failed — leave the pending signals unconsumed so a
        # later run retries against fresh state. The learner is re-runnable.
        logger.info(
            "Feedback learner deferred for (user=%s, target=%s): patch RPC outcome=%s",
            user_id,
            target_id,
            outcome,
        )
        return None
    next_version = new_version

    run_id = str(uuid.uuid4())
    consumed_ids = [r.id for r in pending]
    supabase.table(TABLE).update(
        {
            "applied_at": datetime.now(UTC).isoformat(),
            "applied_run_id": run_id,
        }
    ).in_("id", consumed_ids).execute()

    logger.info(
        "Feedback learner applied for (user=%s, target=%s): +%d negative "
        "keyword(s) %s, %d signals consumed, profile_version=%d",
        user_id,
        target_id,
        len(truly_new),
        truly_new,
        len(consumed_ids),
        next_version,
    )

    return LearnerPatchSummary(
        target_id=target_id,
        applied_run_id=run_id,
        added_negative_keywords=truly_new,
        signals_consumed=len(consumed_ids),
        profile_version_after=next_version,
    )


# ---- Async router drivers (#57 PR-G2d-a) ----------------------------------
# The interactive feedback router is ``async def`` on the pooled async clients,
# but the learner chain stays SYNC — it leans on the shared #191 profile-write
# RPC + ``bulk_score_for_target`` (service-role, poller-shared), which an async
# fork would twin. So the router drives the sync learner through these seams
# instead of holding a sync service client itself (``get_supabase`` acquired here,
# in the service layer, never in the router body).


async def run_learner_off_loop(*, user_id: str, target_id: str) -> LearnerPatchSummary | None:
    """Drive the deterministic learner off the event loop on the pooled sync
    service client, for the ``POST /targets/{id}/learn`` handler (#57 PR-G2d-a)."""
    from app.dependencies import get_supabase

    supabase = get_supabase()
    return await asyncio.to_thread(
        maybe_run_learner, supabase, user_id=user_id, target_id=target_id
    )


def run_learner_and_rescore(supabase: Client, *, user_id: str, target_id: str) -> None:
    """The post-feedback background learner chain (moved from the router's
    ``_safe_run_learner``, #57 PR-G2d-a).

    Run the deterministic learner and, when it actually mutated the profile
    (returned a ``LearnerPatchSummary``), follow with a ``bulk_score_for_target``
    pass so the user sees the lifted/lowered scores on their next page load
    instead of waiting for the next poll cycle. The patch bumped
    ``profile_version`` already, so ``bulk_score_for_target`` only touches rows
    whose ``scored_profile_version`` is now stale.

    Sync + poller-shared; the router drives it off the loop via ``spawn_detached``
    (never a starlette ``BackgroundTask`` — uvloop pool deadlock). Swallows its
    own exceptions (a background failure must not surface as an opaque 500 on a
    later request) while still emitting a usable traceback.
    """
    from app.services.target_scoring import bulk_score_for_target
    from app.services.targets.crud import get as get_target

    try:
        patch = maybe_run_learner(supabase, user_id=user_id, target_id=target_id)
        if patch is None:
            return
        target = get_target(supabase, target_id)
        if target is None:
            return
        n = bulk_score_for_target(supabase, target)
        logger.info(
            "Feedback learner triggered re-score for target=%s: "
            "+%d negatives %s, %d rows re-scored",
            target_id,
            len(patch.added_negative_keywords),
            patch.added_negative_keywords,
            n,
        )
    except Exception:
        logger.exception(
            "Feedback learner failed for (user=%s, target=%s)",
            user_id,
            target_id,
        )


async def run_learner_and_rescore_off_loop(*, user_id: str, target_id: str) -> None:
    """Async entry for the background learner chain: acquire the pooled sync
    service client and run :func:`run_learner_and_rescore` off the event loop.
    The coroutine the ``create_feedback`` handler hands to ``spawn_detached``
    (#57 PR-G2d-a)."""
    from app.dependencies import get_supabase

    supabase = get_supabase()
    await asyncio.to_thread(
        run_learner_and_rescore, supabase, user_id=user_id, target_id=target_id
    )
