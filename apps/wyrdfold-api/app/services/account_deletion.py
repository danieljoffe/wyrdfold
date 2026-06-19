"""Account deletion / right-to-erasure (#29 P1).

Permanently deletes every per-user row and storage object for a user,
then the auth user itself.

**Multi-tenant safe.** The shared catalog — ``jobs``, ``targets``,
``scores``, ``sources`` — is deliberately untouched: those are shared
assets, not the deleting user's personal data. The user's *link* to a
shared target lives in ``user_targets`` (deleted here); the target and
its scores survive for everyone else. This is the key difference from
``scripts/wipe_user_data.py``, a single-tenant clean-slate tool that
deletes ``scores`` and resets shared ``jobs.status`` — both wrong for
multi-tenant erasure.

**Must run with the service-role client.** The cascade crosses
RLS-protected tables and ends in ``auth.admin.delete_user``, neither of
which the JWT-bound user client can do. The calling route authenticates
the user (JWT) and passes the resolved ``user_id``.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from supabase import Client

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
    "user_api_keys",
)


def delete_account(supabase: Client, *, user_id: str) -> dict[str, int]:
    """Erase all data for ``user_id`` and delete the auth user.

    Returns a per-resource count map for the audit log / API response.
    **Idempotent**: re-running deletes nothing a prior run already
    removed (every step is a filtered delete). Order:

    1. storage objects under ``<user_id>/`` in both private buckets;
    2. per-user DB rows (FK-safe; cascades clean up children);
    3. ``notifications_sent`` (keyed by ``user_profiles.id``, not the uid);
    4. the ``user_profiles`` row;
    5. the auth user — last, so a failure there leaves an empty,
       re-onboardable account rather than orphaned data.
    """
    report: dict[str, int] = {}

    # 1. Storage — both buckets namespace objects under <user_id>/.
    report["resume_uploads_objects"] = resume_storage.purge_user_objects(supabase, user_id)
    report["tailored_resume_objects"] = tailored_storage.purge_user_objects(supabase, user_id)

    # 2. Per-user DB rows.
    for table in _USER_ID_TABLES:
        report[table] = _delete_by(supabase, table, "user_id", user_id)

    # 3. notifications_sent is keyed by user_profiles.id, not the auth uid.
    profile_id = _resolve_profile_id(supabase, user_id)
    if profile_id is not None:
        report["notifications_sent"] = _delete_by(
            supabase, "notifications_sent", "user_profile_id", profile_id
        )

    # 4. The profile row itself (also ON DELETE CASCADEs notifications_sent,
    #    a no-op now that step 3 already cleared them).
    report["user_profiles"] = _delete_by(supabase, "user_profiles", "user_id", user_id)

    # 5. Finally the auth account.
    supabase.auth.admin.delete_user(user_id)
    report["auth_user"] = 1

    logger.info("account_deleted user=%s report=%s", user_id, report)
    return report


def _delete_by(supabase: Client, table: str, column: str, value: Any) -> int:
    """Delete rows where ``column == value``; return the count removed.

    Supabase returns the deleted rows by default (``return=representation``),
    so ``len(data)`` is the deleted count — same idiom as
    ``services.keys.store.delete_key``.
    """
    resp = supabase.table(table).delete().eq(column, value).execute()
    return len(resp.data or [])


def _resolve_profile_id(supabase: Client, user_id: str) -> str | None:
    resp = supabase.table("user_profiles").select("id").eq("user_id", user_id).limit(1).execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    return str(rows[0]["id"]) if rows else None
