"""Account deletion / right-to-erasure (#29 P1).

Permanently deletes every per-user row and storage object for a user,
then the auth user itself.

**Multi-tenant safe.** The shared catalog — ``jobs``, ``targets``,
``scores``, ``sources`` — is never *deleted*: those rows are shared
assets, not solely the deleting user's data. The user's *link* to a
shared target lives in ``user_targets`` (deleted here); the target and
its score rows survive for everyone else.

**One exception — scrub, don't delete.** The Phase-2 grader fields on a
``scores`` row (``fit_reasoning``, ``axis_scores``, ``logistics_filters``)
are derived from the grading user's resume — ``fit_reasoning`` quotes
named employers/outcomes (``job_fit.py`` -> ``suggest._profile_summary``).
The row is shared (keyed by ``job_posting_id``+``target_id``, no
``user_id``), so on erasure those fields are nulled for every target the
user was linked to and the row is re-opened for grading (``scoring_status``
-> ``stage2``): the shared row survives but the deleted user's personal
data does not (audit #29 / F1). The numeric ``score`` is left to re-grade.

This is still the key difference from ``scripts/wipe_user_data.py``, a
single-tenant clean-slate tool that *deletes* ``scores`` rows and resets
shared ``jobs.status`` — both wrong for multi-tenant erasure.

**Must run with the service-role client.** The cascade crosses
RLS-protected tables and ends in ``auth.admin.delete_user``, neither of
which the JWT-bound user client can do. The calling route authenticates
the user (JWT) and passes the resolved ``user_id``.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from supabase import AsyncClient

from app.services.ingest import storage as resume_storage
from app.services.tailor import persistence as tailored_storage

logger = logging.getLogger(__name__)

# Per-user tables erased by a single ``.eq("user_id", user_id)`` delete,
# in FK-safe order. Children that ``ON DELETE CASCADE`` off these are
# noted; the parents are still deleted explicitly so the per-table count
# is reported. The shared catalog (jobs / targets / scores / sources) is
# intentionally ABSENT — see the module docstring.
#
# NOTE: ``job_feedback``, ``target_learning_log`` and ``user_targets``
# declare ``user_id`` as TEXT (the rest are UUID); passing the JWT
# ``sub`` string matches both, so no per-table casting is needed.
_USER_ID_TABLES: tuple[str, ...] = (
    "documents",  # document_versions cascades off resume_id
    "uploaded_resumes",
    "experience_optimized_docs",  # experience_chunks cascades off optimized_doc_id
    "experience_prose_docs",
    "experience_conversation_turns",
    "experience_preferences",
    "job_feedback",
    "analyses",
    "llm_costs",
    "target_learning_log",
    "batch_runs",
    "user_jobs",
    "status_log",
    "user_targets",
    # The alert dedup ledger. Keyed by the auth uid since R3 §2 (#557,
    # 20260811010000) — it used to carry a ``user_profiles.id`` surrogate and
    # needed its own ordered step plus a profile lookup to erase.
    "notifications_sent",
    # The user's personal link to a shared ``sources`` row (from-url board
    # registration). Deleted like ``user_targets``: the shared source survives
    # for the corpus, only this user's ownership/attribution is erased.
    "source_registrations",
    "contribution_votes",  # the user's anonymous ref-JD votes (#5 P3)
    "user_api_keys",
    # "I removed this posting from this target" — purely the caller's own
    # curation of their own list. Nothing shared hangs off it (the ``scores``
    # row and the ``jobs`` row both survive for everyone else), so it is a
    # plain delete rather than an anonymize.
    "user_target_job_removals",
)

# Shared tables that carry a ``user_id`` but whose rows are NOT deleted on
# erasure — the user link is nulled instead, so the shared content survives.
# ``reference_jds`` are collective contributions feeding every follower's
# scoring profile; ``merge_by_contributor`` already treats a NULL-user JD as
# the anonymous "system" voice (#5 P2), so anonymizing completes erasure (drops
# the personal link) without perturbing the collective catalog or re-scoring.
_ANONYMIZED_TABLES: tuple[str, ...] = ("reference_jds",)

# ``user_id`` base tables erased outside the ``_USER_ID_TABLES`` loop: the
# profile row, deleted in its own ordered step (5) below.
_OTHER_HANDLED_TABLES: tuple[str, ...] = ("user_profiles",)

# Every ``public`` base table with a ``user_id`` column must fall in exactly
# one bucket — deleted, anonymized, or the explicitly-handled profile row.
# ``tests/integration/test_erasure_coverage.py`` asserts the live schema never
# grows a ``user_id`` table absent from here, so a new per-user table can't
# silently slip erasure (the gap that left reference_jds/contribution_votes
# behind, #29).
ERASURE_HANDLED_USER_ID_TABLES: frozenset[str] = frozenset(
    _USER_ID_TABLES + _ANONYMIZED_TABLES + _OTHER_HANDLED_TABLES
)

# Phase-2 LLM grader outputs on the shared ``scores`` row that are derived
# from the grading user's resume payload. ``fit_reasoning`` quotes named
# employers/outcomes; ``axis_scores``/``logistics_filters`` encode the same
# per-user assessment. The row is shared (no ``user_id``) so it is scrubbed,
# not deleted — see the module docstring and ``_scrub_shared_scores``.
_SCORE_PII_COLUMNS: tuple[str, ...] = (
    "fit_reasoning",
    "axis_scores",
    "logistics_filters",
)


async def delete_account(supabase: AsyncClient, *, user_id: str) -> dict[str, int]:
    """Erase all data for ``user_id`` and delete the auth user.

    Returns a per-resource count map for the audit log / API response.
    **Idempotent**: re-running removes nothing a prior run already cleared
    (every step is a filtered delete/update). Order:

    1. storage objects under ``<user_id>/`` in both private buckets;
    2. per-user DB rows (FK-safe; cascades clean up children);
    2b. anonymize shared ``reference_jds`` (null the user link, keep the JD —
       collective content, not personal data; see ``_ANONYMIZED_TABLES``);
    3. scrub the user's derived PII from shared ``scores`` rows — the rows
       survive (shared catalog), only the Phase-2 grader fields are nulled
       (see the module docstring);
    4. the ``user_profiles`` row;
    5. the auth user — last, so a failure there leaves an empty,
       re-onboardable account rather than orphaned data.

    ``notifications_sent`` used to need its own ordered step here because it
    was keyed by ``user_profiles.id``. R3 §2 (#557) repointed it at the auth
    uid, so it erases in the step-2 loop like every other per-user table.
    """
    report: dict[str, int] = {}

    # Capture the user's target links BEFORE step 2 deletes ``user_targets``,
    # so step 3 can scrub their derived PII from the shared scores rows.
    target_ids = await _user_target_ids(supabase, user_id)

    # 1. Storage — both buckets namespace objects under <user_id>/.
    report["resume_uploads_objects"] = await resume_storage.purge_user_objects(supabase, user_id)
    report["tailored_resume_objects"] = await tailored_storage.purge_user_objects(supabase, user_id)

    # 2. Per-user DB rows (incl. the user's anonymous ref-JD votes).
    for table in _USER_ID_TABLES:
        report[table] = await _delete_by(supabase, table, "user_id", user_id)

    # 2b. Anonymize the user's shared reference-JD contributions: keep the JD
    #     content in the collective catalog, null only the personal link (merge
    #     already treats NULL-user JDs as the "system" voice, #5 P2). Deleting
    #     the votes above can leave a contribution's ``suppressed`` flag
    #     momentarily stale, but it stays consistent with the already-merged
    #     profile and self-heals on the next vote (which re-tallies + re-merges).
    report["reference_jds_anonymized"] = await _anonymize_user_id(
        supabase, "reference_jds", user_id
    )

    # 3. Scrub this user's PII off the shared scores rows for their targets.
    report["scores_scrubbed"] = await _scrub_shared_scores(supabase, target_ids)

    # 3b. Reap any of those targets this user was the LAST follower of (#667).
    #     Step 2 deleted the ``user_targets`` rows but deliberately leaves the
    #     shared target — co-followers may remain. When none do, the row becomes
    #     unreachable: every target route is membership-scoped, so it 404s for
    #     everyone, while still holding all its scores. The request-path unlink
    #     reaps for exactly this reason; erasure deletes memberships by a
    #     different route and used to skip it.
    #
    #     ``reap_orphaned_target`` re-checks both guards server-side (no
    #     memberships left, no ops sponsorship) in the same snapshot as the
    #     delete, so passing a target another user still follows is a no-op
    #     rather than a cross-tenant deletion. That is what makes it safe to
    #     call on every id the user was linked to.
    reaped = 0
    for target_id in target_ids:
        if await _reap_orphaned_target(supabase, target_id):
            reaped += 1
    report["targets_reaped"] = reaped

    # 4. The profile row itself.
    report["user_profiles"] = await _delete_by(supabase, "user_profiles", "user_id", user_id)

    # 5. Finally the auth account.
    await supabase.auth.admin.delete_user(user_id)
    report["auth_user"] = 1

    logger.info("account_deleted user=%s report=%s", user_id, report)
    return report


async def _delete_by(supabase: AsyncClient, table: str, column: str, value: Any) -> int:
    """Delete rows where ``column == value``; return the count removed.

    Supabase returns the deleted rows by default (``return=representation``),
    so ``len(data)`` is the deleted count — same idiom as
    ``services.keys.store.delete_key``.
    """
    resp = await supabase.table(table).delete().eq(column, value).execute()
    return len(resp.data or [])


async def _anonymize_user_id(supabase: AsyncClient, table: str, user_id: str) -> int:
    """NULL the ``user_id`` on a shared table's rows for the user — the row
    (shared content) survives, only the personal link is removed. Returns the
    count anonymized. Idempotent: a second run matches nothing (already NULL).
    """
    resp = await supabase.table(table).update({"user_id": None}).eq("user_id", user_id).execute()
    return len(resp.data or [])


async def _user_target_ids(supabase: AsyncClient, user_id: str) -> list[str]:
    """The shared-target ids this user is linked to via ``user_targets``."""
    resp = await supabase.table("user_targets").select("target_id").eq("user_id", user_id).execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    return [tid for r in rows if (tid := r.get("target_id"))]


async def _reap_orphaned_target(supabase: AsyncClient, target_id: str) -> bool:
    """Remove a shared target this user was the last follower of (#667).

    Thin wrapper over the same guarded RPC the request-path unlink uses, so the
    two erasure routes cannot drift. The guards (no memberships remain, no
    ops sponsorship) live in SQL and are evaluated in the same snapshot as the
    delete — critical here, because erasure hands it EVERY target the user was
    linked to, most of which other people still follow.

    Fail-soft: erasure has already removed the user's own data by this point and
    must not abort partway on a tidy-up step. A failure leaves an orphan, which
    is the pre-existing behaviour, not a regression.
    """
    try:
        resp = await supabase.rpc("reap_orphaned_target", {"p_target_id": target_id}).execute()
    except Exception:
        logger.warning(
            "account_deletion: reap_orphaned_target failed for %s", target_id, exc_info=True
        )
        return False
    return bool(resp.data)


async def _scrub_shared_scores(supabase: AsyncClient, target_ids: list[str]) -> int:
    """Null the Phase-2 personal fields on shared ``scores`` rows for the
    user's targets and re-open them for grading; return rows updated.

    The rows are NOT deleted (shared catalog) — only the deleting user's
    derived PII is cleared. ``scoring_status`` -> ``stage2`` re-admits the
    row so the poller re-grades it from a current subscriber's profile; a
    target with no remaining subscriber simply stays dormant and nulled.
    No-op (and no ``.in_([])``) when the user had no targets.
    """
    if not target_ids:
        return 0
    update_payload: dict[str, Any] = dict.fromkeys(_SCORE_PII_COLUMNS, None)
    update_payload["scoring_status"] = "stage2"
    resp = (
        await supabase.table("scores").update(update_payload).in_("target_id", target_ids).execute()
    )
    return len(resp.data or [])
