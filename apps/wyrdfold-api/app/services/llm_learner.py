"""LLM-driven feedback learner (Doc 2 v2).

Layered on the v1 deterministic learner (``app.services.feedback``) — v1's
literal-token path handles the obvious case ("3 users marked 'sales rep'
irrelevant"); v2 takes the rest. The LLM reads job title + reason on
unapplied feedback rows alongside the current scoring profile and emits
a ``ProfilePatch`` (add/remove negatives, add secondaries, demote
keywords). High-confidence patches auto-apply; low-confidence stage in
``target_learning_log`` for user review.

Cost: one Sonnet call per learn-run per (user, target) — same model the
existing ``derive_profile_from_label`` pipeline uses. The system prompt
is cacheable so repeated runs on the same target only pay variable-tokens.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from supabase import Client

from app.models.feedback import FeedbackRow
from app.models.learning import (
    CONFIDENCE_AUTO_APPLY,
    LearningRunResult,
    LearningStatus,
    ProfilePatch,
    TargetLearningLogRow,
)
from app.models.llm import Message, ModelId
from app.services.feedback import _MIN_FEEDBACK_FOR_LEARN, _parse_row
from app.services.llm.client import LLMClient, complete_json
from app.services.llm.cost_log import enqueue as enqueue_llm_cost
from app.services.llm.untrusted import UNTRUSTED_CONTENT_DIRECTIVE, wrap_untrusted
from app.services.targets import crud as targets_crud

# Imported under the historical private name so existing test patch points
# (``app.services.llm_learner._project_patch_impact``) keep working.
from app.services.targets.learning_projection import (
    project_profile_impact as _project_patch_impact,
)
from app.services.targets.merge import merge_reference_jds
from app.services.targets.profile_writes import (
    apply_profile_merge_rpc,
    apply_profile_patch_rpc,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL: ModelId = "claude-sonnet-4-6"
DEFAULT_PURPOSE = "target.learn_from_feedback"

LEARNING_LOG_TABLE = "target_learning_log"


class StagedPatchConflictError(Exception):
    """Applying a staged patch lost a concurrent-write race on the shared
    target profile (`version_conflict` from the RPC). The caller should
    re-review the staged patch against the now-current profile."""

SYSTEM_PROMPT = (
    UNTRUSTED_CONTENT_DIRECTIVE
    + "\n\n"
    + """\
You are a job-search relevance learner. Given a user's scoring profile \
for one of their target roles and a batch of relevance feedback signals \
they have left on individual job postings, return a minimal ``ProfilePatch`` \
that adjusts the profile to better match their preferences going forward.

Your goal is precision, not completeness. A single misclick is noise — \
only patch the profile when at least 2 distinct feedback rows agree on \
the same underlying pattern. Prefer surgical edits to the negative list \
over rewriting categories.

Rules:
- ``add_negative`` is for keywords that, when present in a JOB TITLE or \
  REQUIREMENTS section, should disqualify the posting. Prefer single \
  words over phrases (the matcher normalizes word boundaries). Example: \
  the user marks three Sales Rep postings irrelevant with reason "sales \
  role" — add "sales". Don't add the user's own role title here.
- ``remove_negative`` is for keywords you can show are over-rejecting — \
  this fires when positive feedback contradicts an existing negative. \
  Rare; leave empty unless the evidence is unambiguous.
- ``add_secondary`` adds keywords to ``secondary_skills`` (weight 1-3) \
  when positive feedback consistently mentions a skill not yet in the \
  profile. Skip if it's already in ``core_skills`` — promote that \
  separately.
- ``demote_keywords`` removes a keyword from any category. Use when a \
  keyword is clearly causing false-positive matches.

``confidence`` is a 0..1 estimate of how sure you are the patch reflects \
the user's actual preferences (vs. noise from a single bad day). Values \
< 0.6 will be staged for manual review rather than applied; calibrate \
honestly. ``rationale`` is one paragraph for the audit log explaining \
which feedback rows drove which fields — be specific.

Return an empty patch (all collections empty) with ``confidence`` ≥ 0.8 \
if the feedback batch has no learnable pattern. The system will stamp \
the rows as consumed without mutating the profile, which is the correct \
outcome for "everything was misclick".
"""
)


def _build_user_message(
    profile: dict[str, Any],
    feedback: list[FeedbackRow],
    job_titles: dict[str, str],
) -> Message:
    rows_payload: list[dict[str, Any]] = []
    for row in feedback:
        # ``title`` (scraped) and ``reason`` (user-typed) are attacker-
        # controllable and feed a patch to the SHARED scoring profile, so a
        # malicious note here could poison every user on the target. Fence both
        # values — json.dumps then escapes the JSON, and wrap_untrusted defangs
        # any forged fence inside the value, so it stays inert data.
        rows_payload.append(
            {
                "signal": row.signal,
                "title": wrap_untrusted(
                    job_titles.get(row.job_posting_id, "?"),
                    name="feedback",
                    block=False,
                ),
                "reason": wrap_untrusted(row.reason or "", name="feedback", block=False),
            }
        )
    body = {
        "current_scoring_profile": profile,
        "feedback_rows": rows_payload,
    }
    return Message(
        role="user",
        content=(
            "Current target profile and the user's feedback rows:\n\n"
            f"{json.dumps(body, indent=2)}\n\n"
            "Return a ProfilePatch that reflects the most defensible "
            "adjustments. Be conservative."
        ),
    )


def _fetch_unapplied_feedback(
    supabase: Client, user_id: str, target_id: str, limit: int = 50
) -> list[FeedbackRow]:
    resp = (
        supabase.table("job_feedback")
        .select("*")
        .eq("user_id", user_id)
        .eq("target_id", target_id)
        .is_("applied_at", "null")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return [_parse_row(r) for r in rows]


def _fetch_job_titles(
    supabase: Client, job_ids: list[str]
) -> dict[str, str]:
    if not job_ids:
        return {}
    # Deduplicate to keep the in_() filter sensible at scale.
    unique = list({jid for jid in job_ids if jid})
    resp = (
        supabase.table("jobs")
        .select("id, title")
        .in_("id", unique)
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    return {r["id"]: r.get("title", "?") for r in rows}


def _core_skill_keywords(profile: dict[str, Any]) -> list[str]:
    """The target's own ``core_skills`` keyword names from the scoring profile."""
    cats = profile.get("categories") or {}
    core = (cats.get("core_skills") or {}).get("keywords") or {}
    return list(core) if isinstance(core, dict) else []


def _strip_self_colliding_negatives(
    patch: ProfilePatch,
    *,
    search_keywords: list[str],
    core_skills: list[str],
) -> tuple[ProfilePatch, list[str]]:
    """Drop any ``add_negative`` that would hard-zero the target's OWN jobs (#47).

    A negative keyword in a job title is a *hard exclude* (score → 0). The
    learner's prompt says "don't add the user's own role title," but nothing
    enforced it, so a mis-attributed negative (e.g. "success" learned from
    "Customer Success" feedback) could nuke a whole slice of legitimate roles
    that then never resurface. We apply the EXACT rule the negative matcher uses
    (``scoring.py``: ``\\bkeyword\\b``): a candidate is self-colliding if, word-
    boundary matched, it hits one of the target's own ``search_keywords`` or
    ``core_skills``. Returns the cleaned patch + the dropped keywords (for the
    audit log). Given the asymmetric "lost forever" downside, dropping a
    borderline negative beats hard-zeroing real jobs — the learner can re-propose.
    """
    if not patch.add_negative:
        return patch, []
    protected = [s.lower() for s in (*search_keywords, *core_skills) if s]
    if not protected:
        return patch, []
    kept: list[str] = []
    dropped: list[str] = []
    for kw in patch.add_negative:
        norm = kw.strip().lower()
        pattern = re.compile(rf"\b{re.escape(norm)}\b") if norm else None
        if pattern is not None and any(pattern.search(p) for p in protected):
            dropped.append(kw)
        else:
            kept.append(kw)
    if not dropped:
        return patch, []
    return patch.model_copy(update={"add_negative": kept}), dropped


def _apply_patch_to_profile(
    profile: dict[str, Any], patch: ProfilePatch
) -> dict[str, Any]:
    """Pure function: returns a NEW profile dict with the patch applied.

    Leaves the input untouched so the caller can use it as ``prev_profile``
    in the audit log.
    """
    next_profile = json.loads(json.dumps(profile))  # deep copy via JSON

    # Negative keywords
    negative = next_profile.setdefault("negative", {})
    negative.setdefault("weight", -10.0)
    existing_neg: list[str] = negative.setdefault("keywords", [])
    existing_neg_set = {kw.lower() for kw in existing_neg}
    for kw in patch.add_negative:
        if kw.lower() not in existing_neg_set:
            existing_neg.append(kw)
            existing_neg_set.add(kw.lower())
    if patch.remove_negative:
        drop = {kw.lower() for kw in patch.remove_negative}
        negative["keywords"] = [kw for kw in existing_neg if kw.lower() not in drop]

    # Secondary skills
    if patch.add_secondary:
        categories = next_profile.setdefault("categories", {})
        secondary = categories.setdefault("secondary_skills", {})
        secondary.setdefault("weight", 1.0)
        keywords = secondary.setdefault("keywords", {})
        for kw, weight in patch.add_secondary.items():
            if kw not in keywords:
                keywords[kw] = max(1, min(3, int(weight)))

    # Demotions — remove from any category that holds them.
    if patch.demote_keywords:
        drop_set = {kw.lower() for kw in patch.demote_keywords}
        for _cat_name, cat in (next_profile.get("categories") or {}).items():
            kws = cat.get("keywords") or {}
            if isinstance(kws, dict):
                cat["keywords"] = {
                    k: v for k, v in kws.items() if k.lower() not in drop_set
                }

    return cast(dict[str, Any], next_profile)


def _insert_log(
    supabase: Client,
    *,
    user_id: str,
    target_id: str,
    status: LearningStatus,
    prev_profile: dict[str, Any],
    next_profile: dict[str, Any],
    patch: ProfilePatch,
    signals_consumed: int,
    applied_run_id: str | None,
    projection: dict[str, Any] | None = None,
) -> TargetLearningLogRow:
    payload: dict[str, Any] = {
        "user_id": user_id,
        "target_id": target_id,
        "status": status,
        "prev_profile": prev_profile,
        "next_profile": next_profile,
        "diff": patch.model_dump(mode="json"),
        "confidence": round(patch.confidence, 2),
        "rationale": patch.rationale,
        "signals_consumed": signals_consumed,
        "applied_run_id": applied_run_id,
        "projection": projection,
    }
    resp = supabase.table(LEARNING_LOG_TABLE).insert(payload).execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        raise RuntimeError("Failed to insert target_learning_log row")
    return TargetLearningLogRow.model_validate(rows[0])


def _stamp_consumed_feedback(
    supabase: Client, feedback_ids: list[str], run_id: str
) -> None:
    if not feedback_ids:
        return
    supabase.table("job_feedback").update(
        {
            "applied_at": datetime.now(UTC).isoformat(),
            "applied_run_id": run_id,
        }
    ).in_("id", feedback_ids).execute()


def _is_empty_patch(patch: ProfilePatch) -> bool:
    return (
        not patch.add_negative
        and not patch.remove_negative
        and not patch.add_secondary
        and not patch.demote_keywords
    )


async def run_llm_learner(
    supabase: Client,
    llm: LLMClient,
    *,
    user_id: str,
    target_id: str,
) -> LearningRunResult | None:
    """Run one LLM learn pass for (user, target).

    Returns ``None`` when there's nothing to learn from (below threshold).
    Returns a ``LearningRunResult`` when the LLM produced a patch — applied
    or staged depending on confidence. An "empty patch high confidence"
    response is treated as a no-op apply: feedback rows are stamped
    consumed so we don't keep re-asking the LLM about the same noise.
    """
    feedback = _fetch_unapplied_feedback(supabase, user_id, target_id)
    if len(feedback) < _MIN_FEEDBACK_FOR_LEARN:
        return None

    target_resp = await asyncio.to_thread(
        lambda: supabase.table("targets")
        .select("*")
        .eq("id", target_id)
        .single()
        .execute()
    )
    target_row = cast(dict[str, Any] | None, target_resp.data)
    if target_row is None:
        return None
    prev_profile = cast(dict[str, Any], target_row.get("scoring_profile") or {})

    job_titles = _fetch_job_titles(
        supabase, [r.job_posting_id for r in feedback]
    )

    patch, llm_result = await complete_json(
        llm,
        model=DEFAULT_MODEL,
        system=SYSTEM_PROMPT,
        messages=[_build_user_message(prev_profile, feedback, job_titles)],
        schema=ProfilePatch,
        purpose=DEFAULT_PURPOSE,
        cache_system=True,
    )
    enqueue_llm_cost(user_id, DEFAULT_PURPOSE, llm_result)

    # Before anything else, drop any proposed negative that would hard-zero the
    # target's OWN jobs (a negative matching its search keywords / core skills).
    # The prompt asks the model not to, but it's a hard-exclude with a "lost
    # forever" downside, so enforce it in code (#47).
    search_keywords = cast(list[str], target_row.get("search_keywords") or [])
    patch, dropped_negatives = _strip_self_colliding_negatives(
        patch,
        search_keywords=search_keywords,
        core_skills=_core_skill_keywords(prev_profile),
    )
    if dropped_negatives:
        logger.warning(
            "LLM learner dropped self-colliding negative(s) %s for "
            "(user=%s, target=%s) — they match the target's own search "
            "keywords/core skills and would hard-zero legitimate jobs",
            dropped_negatives,
            user_id,
            target_id,
        )
        patch = patch.model_copy(
            update={
                "rationale": (
                    f"[dropped self-colliding negatives {dropped_negatives}] "
                    + patch.rationale
                )
            }
        )

    run_id = str(uuid.uuid4())
    feedback_ids = [r.id for r in feedback]

    # Empty patch with high confidence: nothing to apply, but stamp the
    # feedback rows so we don't keep paying for the same Sonnet round-trip.
    if _is_empty_patch(patch):
        log = _insert_log(
            supabase,
            user_id=user_id,
            target_id=target_id,
            status="applied",
            prev_profile=prev_profile,
            next_profile=prev_profile,
            patch=patch,
            signals_consumed=len(feedback_ids),
            applied_run_id=run_id,
        )
        _stamp_consumed_feedback(supabase, feedback_ids, run_id)
        return LearningRunResult(
            log=log,
            applied=True,
            profile_version_after=cast(int, target_row.get("profile_version") or 1),
        )

    next_profile = _apply_patch_to_profile(prev_profile, patch)

    if patch.confidence < CONFIDENCE_AUTO_APPLY:
        # Stage for review — do NOT mutate the target or stamp feedback.
        log = _insert_log(
            supabase,
            user_id=user_id,
            target_id=target_id,
            status="staged",
            prev_profile=prev_profile,
            next_profile=next_profile,
            patch=patch,
            signals_consumed=len(feedback_ids),
            applied_run_id=None,
        )
        return LearningRunResult(log=log, applied=False)

    # High confidence — but before auto-applying, project the patch over the
    # target's recent scored jobs and stage it instead if it would churn an
    # outlier share of the list (the learning-rate cap, #5 P4). Off-loaded to
    # a thread: it fetches + deterministically re-scores up to N jobs.
    # (``search_keywords`` was resolved above for the negative-collision guard.)
    projection = await asyncio.to_thread(
        _project_patch_impact,
        supabase,
        target_id,
        prev_profile,
        next_profile,
        search_keywords,
    )
    projection_json = projection.model_dump(mode="json") if projection else None

    if projection is not None and projection.capped:
        note = (
            f"[auto-staged by the learning-rate cap: this patch would move "
            f"{projection.jobs_moved}/{projection.jobs_considered} recent jobs "
            f"by ≥{projection.move_threshold} pts, over the "
            f"{projection.max_moved_fraction:.0%} cap] "
        )
        staged_patch = patch.model_copy(
            update={"rationale": note + patch.rationale}
        )
        log = _insert_log(
            supabase,
            user_id=user_id,
            target_id=target_id,
            status="staged",
            prev_profile=prev_profile,
            next_profile=next_profile,
            patch=staged_patch,
            signals_consumed=len(feedback_ids),
            applied_run_id=None,
            projection=projection_json,
        )
        logger.info(
            "LLM learner OUTLIER-staged for (user=%s, target=%s): conf=%.2f "
            "but projected to move %d/%d jobs ≥%d pts (cap %.0f%%)",
            user_id,
            target_id,
            patch.confidence,
            projection.jobs_moved,
            projection.jobs_considered,
            projection.move_threshold,
            projection.max_moved_fraction * 100,
        )
        return LearningRunResult(log=log, applied=False)

    # Apply — through the SECURITY DEFINER RPC (#191): the DB re-checks the
    # follower link and that the profile hasn't moved since we computed the
    # patch, under a row lock.
    expected_version = cast(int, target_row.get("profile_version") or 1)
    outcome, rpc_version = await asyncio.to_thread(
        lambda: apply_profile_patch_rpc(
            supabase,
            user_id=user_id,
            target_id=target_id,
            next_profile=next_profile,
            expected_version=expected_version,
        )
    )
    if outcome == "version_conflict":
        # Another write (reference-JD merge, concurrent learn) landed after
        # we read the profile — next_profile and the projection are stale.
        # Stage for review instead of clobbering the newer profile; feedback
        # stays unstamped so a re-run can revisit it.
        note = (
            "[auto-staged: the shared profile changed during the learn run "
            f"(expected v{expected_version}, found v{rpc_version})] "
        )
        staged_patch = patch.model_copy(
            update={"rationale": note + patch.rationale}
        )
        log = _insert_log(
            supabase,
            user_id=user_id,
            target_id=target_id,
            status="staged",
            prev_profile=prev_profile,
            next_profile=next_profile,
            patch=staged_patch,
            signals_consumed=len(feedback_ids),
            applied_run_id=None,
            projection=projection_json,
        )
        logger.warning(
            "LLM learner apply lost the version race for (user=%s, "
            "target=%s): expected v%d, found v%s — staged for review",
            user_id,
            target_id,
            expected_version,
            rpc_version,
        )
        return LearningRunResult(log=log, applied=False)
    if outcome != "applied":
        # not_a_follower / target_not_found — the router pre-checks both, so
        # reaching here means the link or target was severed mid-run (or a
        # future caller skipped the check). Nothing safe to write.
        logger.warning(
            "LLM learner apply refused by RPC (%s) for (user=%s, target=%s)",
            outcome,
            user_id,
            target_id,
        )
        return None
    new_version = cast(int, rpc_version)

    log = _insert_log(
        supabase,
        user_id=user_id,
        target_id=target_id,
        status="applied",
        prev_profile=prev_profile,
        next_profile=next_profile,
        patch=patch,
        signals_consumed=len(feedback_ids),
        applied_run_id=run_id,
        projection=projection_json,
    )
    _stamp_consumed_feedback(supabase, feedback_ids, run_id)

    logger.info(
        "LLM learner applied for (user=%s, target=%s): +%d neg, +%d sec, "
        "-%d demoted, conf=%.2f, profile_version=%d",
        user_id,
        target_id,
        len(patch.add_negative),
        len(patch.add_secondary),
        len(patch.demote_keywords),
        patch.confidence,
        new_version,
    )
    return LearningRunResult(
        log=log, applied=True, profile_version_after=new_version
    )


def _apply_staged_merge(
    supabase: Client,
    *,
    user_id: str,
    log_row: dict[str, Any],
    target_row: dict[str, Any],
) -> LearningRunResult | None:
    """Approve a quarantined reference-JD merge (#191 slice 1b).

    The staged row's contribution was inserted suppressed-at-birth because
    its projected impact tripped the learning-rate cap. Approval means:
    include it in a FRESH merge over the target's current contributions
    (never the stage-time snapshot — other contributions may have come or
    gone since), write through the merge RPC, and only then lift the
    suppression flag. A refused/crashed write leaves the quarantine intact
    (the next re-merge self-heals back to the suppressed state).
    """
    target_id = cast(str, log_row["target_id"])
    payload = cast(dict[str, Any], log_row.get("merge_payload") or {})
    ref_jd_id = payload.get("ref_jd_id")

    ref_jds = targets_crud.list_reference_jds(supabase, target_id)
    if ref_jd_id is not None and not any(j.id == ref_jd_id for j in ref_jds):
        # The quarantined contribution was deleted while staged — nothing
        # to approve. Surfaced as the router's 404.
        return None
    # Include the quarantined row in the merge WITHOUT flipping the DB flag
    # yet — the flag lifts only after the RPC accepts the write.
    unquarantined = [
        j.model_copy(update={"suppressed": False}) if j.id == ref_jd_id else j
        for j in ref_jds
    ]
    prev_profile = cast(dict[str, Any], target_row.get("scoring_profile") or {})
    next_profile = merge_reference_jds(unquarantined).model_dump()

    expected_version = cast(int, target_row.get("profile_version") or 1)
    outcome, rpc_version = apply_profile_merge_rpc(
        supabase,
        user_id=user_id,
        target_id=target_id,
        next_profile=next_profile,
        expected_version=expected_version,
        search_keywords=payload.get("search_keywords"),
        example_promising=payload.get("example_promising_titles"),
        example_unpromising=payload.get("example_unpromising_titles"),
    )
    if outcome == "version_conflict":
        raise StagedPatchConflictError(
            f"target {target_id}: profile moved to v{rpc_version} while "
            f"applying staged merge {log_row['id']} (read v{expected_version})"
        )
    if outcome != "applied":
        logger.warning(
            "Staged-merge apply refused by RPC (%s) for (user=%s, target=%s)",
            outcome,
            user_id,
            target_id,
        )
        return None
    new_version = cast(int, rpc_version)

    if ref_jd_id is not None:
        supabase.table("reference_jds").update({"suppressed": False}).eq(
            "id", ref_jd_id
        ).eq("target_id", target_id).execute()

    new_run_id = str(uuid.uuid4())
    update_resp = (
        supabase.table(LEARNING_LOG_TABLE)
        .update(
            {
                "status": "applied",
                "applied_run_id": new_run_id,
                "prev_profile": prev_profile,
                "next_profile": next_profile,
            }
        )
        .eq("id", log_row["id"])
        .execute()
    )
    rows = cast(list[dict[str, Any]], update_resp.data or [])
    if not rows:
        return None
    # No feedback stamping — a merge consumes no feedback signals.
    return LearningRunResult(
        log=TargetLearningLogRow.model_validate(rows[0]),
        applied=True,
        profile_version_after=new_version,
    )


def apply_staged_patch(
    supabase: Client, *, user_id: str, run_id: str
) -> LearningRunResult | None:
    """Take a staged patch + apply it. Returns None if no staged row matches."""
    log_resp = (
        supabase.table(LEARNING_LOG_TABLE)
        .select("*")
        .eq("id", run_id)
        .eq("user_id", user_id)
        .eq("status", "staged")
        .single()
        .execute()
    )
    log_row = cast(dict[str, Any] | None, log_resp.data)
    if log_row is None:
        return None

    target_id = log_row["target_id"]
    target_resp = (
        supabase.table("targets")
        .select("*")
        .eq("id", target_id)
        .single()
        .execute()
    )
    target_row = cast(dict[str, Any] | None, target_resp.data)
    if target_row is None:
        return None

    if log_row.get("kind") == "merge":
        # A quarantined reference-JD merge (#191 slice 1b) — approving it
        # lifts the contribution's suppression and re-merges fresh.
        return _apply_staged_merge(
            supabase,
            user_id=user_id,
            log_row=log_row,
            target_row=target_row,
        )

    # Re-apply the PATCH to the CURRENT profile rather than writing the
    # stage-time `next_profile` snapshot (Copilot on #202): the profile may
    # have legitimately moved since staging (a reference-JD merge, another
    # learn), and writing the stale snapshot would wholesale-erase those
    # changes. The human approved the patch's *edits*, not a revert of
    # everything that landed since. The log keeps `diff` = the full
    # ProfilePatch, so this is lossless.
    prev_profile = cast(dict[str, Any], target_row.get("scoring_profile") or {})
    stage_patch = ProfilePatch.model_validate(log_row["diff"])
    # Re-run the self-collision guard against the CURRENT target terms —
    # search keywords / core skills may have changed since staging, and a
    # negative that now hard-zeros the target's own jobs is the same "lost
    # forever" hazard the stage-time strip protects against (#47).
    stage_patch, dropped_negatives = _strip_self_colliding_negatives(
        stage_patch,
        search_keywords=cast(
            list[str], target_row.get("search_keywords") or []
        ),
        core_skills=_core_skill_keywords(prev_profile),
    )
    if dropped_negatives:
        logger.warning(
            "Staged-patch apply dropped now-self-colliding negative(s) %s "
            "for (user=%s, target=%s)",
            dropped_negatives,
            user_id,
            target_id,
        )
        # Make the shrink self-explanatory in the audit log / UI — same
        # bracketed-note convention as the auto-stage paths.
        stage_patch = stage_patch.model_copy(
            update={
                "rationale": (
                    f"[apply dropped now-self-colliding negatives "
                    f"{dropped_negatives}] " + stage_patch.rationale
                )
            }
        )
    next_profile = _apply_patch_to_profile(prev_profile, stage_patch)

    # Same RPC gate as the auto-apply path (#191). expected_version is the
    # version prev_profile was read at, so the recompute-to-write window is
    # fully covered — a conflict means a genuinely concurrent write landed
    # mid-apply, and retrying (the router's 409) recomputes from fresh state.
    expected_version = cast(int, target_row.get("profile_version") or 1)
    outcome, rpc_version = apply_profile_patch_rpc(
        supabase,
        user_id=user_id,
        target_id=target_id,
        next_profile=next_profile,
        expected_version=expected_version,
    )
    if outcome == "version_conflict":
        raise StagedPatchConflictError(
            f"target {target_id}: profile moved to v{rpc_version} while "
            f"applying staged run {run_id} (read v{expected_version})"
        )
    if outcome != "applied":
        # not_a_follower / target_not_found — surfaced as the router's 404.
        logger.warning(
            "Staged-patch apply refused by RPC (%s) for (user=%s, target=%s)",
            outcome,
            user_id,
            target_id,
        )
        return None
    new_version = cast(int, rpc_version)

    # Mark this run as applied + stamp the consumed feedback rows. The run
    # id we generate here is what gets attached to the feedback so the
    # audit thread links back to a single applied event even though the
    # log row was created earlier with status=staged. prev/next/diff (and
    # the rationale, when the strip dropped negatives) are updated to what
    # was ACTUALLY applied — the UI renders `diff`, and `prev_profile` must
    # stay a truthful one-row revert (Copilot on #203).
    new_run_id = str(uuid.uuid4())
    update_resp = (
        supabase.table(LEARNING_LOG_TABLE)
        .update(
            {
                "status": "applied",
                "applied_run_id": new_run_id,
                "prev_profile": prev_profile,
                "next_profile": next_profile,
                "diff": stage_patch.model_dump(mode="json"),
                "rationale": stage_patch.rationale,
            }
        )
        .eq("id", run_id)
        .execute()
    )
    rows = cast(list[dict[str, Any]], update_resp.data or [])
    if not rows:
        return None

    # Consume any feedback that's still unapplied for this target — these
    # are the rows that fed the original staged patch. If new feedback has
    # arrived since the stage, it gets consumed here too, which is the
    # correct behavior (the staged patch was the user's last decision).
    pending_resp = (
        supabase.table("job_feedback")
        .select("id")
        .eq("user_id", user_id)
        .eq("target_id", target_id)
        .is_("applied_at", "null")
        .execute()
    )
    pending_rows = cast(list[dict[str, Any]], pending_resp.data or [])
    pending_ids = [r["id"] for r in pending_rows]
    _stamp_consumed_feedback(supabase, pending_ids, new_run_id)

    return LearningRunResult(
        log=TargetLearningLogRow.model_validate(rows[0]),
        applied=True,
        profile_version_after=new_version,
    )


def reject_staged_patch(
    supabase: Client, *, user_id: str, run_id: str
) -> LearningRunResult | None:
    """Mark a staged patch as rejected. Does NOT stamp feedback as
    consumed — those rows stay unapplied so a future learn run can
    revisit them (the user said "no" to this *interpretation*, not to
    the underlying signal).

    For ``kind='merge'`` rows the status flip is the whole story: the
    quarantined contribution simply STAYS suppressed (#191 slice 1b) —
    excluded from every merge — though the vote quorum can still rescue
    it later through the normal suppression machinery."""
    resp = (
        supabase.table(LEARNING_LOG_TABLE)
        .update({"status": "rejected"})
        .eq("id", run_id)
        .eq("user_id", user_id)
        .eq("status", "staged")
        .execute()
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        return None
    return LearningRunResult(
        log=TargetLearningLogRow.model_validate(rows[0]),
        applied=False,
    )
