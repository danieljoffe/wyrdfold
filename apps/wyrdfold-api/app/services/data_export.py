"""Personal-data export / portability (#29 P2).

Builds a single ZIP of everything a user has given us:

* ``data.json`` — every per-user DB row, grouped by table. Stored API
  keys are projected **without the ciphertext** (provider + ``last4``
  only); derived vector embeddings are omitted (they live in
  ``experience_chunks``, which cascades off the optimized doc and carries
  no user-authored content).
* ``files/<bucket>/<name>`` — the original uploaded resumes and generated
  documents from both Storage buckets.
* ``README.txt`` — a manifest with per-table row counts.

Runs with the **service-role** client, scoped by ``user_id`` (and the
resolved ``user_profiles.id`` for ``notifications_sent``) — same trust
model as account deletion: the route is JWT-gated and a user only ever
exports their own rows. The export inventory is kept in lockstep with the
deletion inventory (see ``app.services.account_deletion`` and
``test_data_export``) so "download everything" and "delete everything"
cover the same data.
"""

from __future__ import annotations

import io
import json
import logging
import zipfile
from datetime import UTC, datetime
from typing import Any, cast

from supabase import Client

from app.services.ingest import storage as resume_storage
from app.services.tailor import persistence as tailored_storage

logger = logging.getLogger(__name__)

# Per-user tables exported with SELECT *, keyed by ``user_id``. Mirrors
# account_deletion._USER_ID_TABLES (a lockstep test guards drift) so the
# export and the erasure cascade always cover the same rows.
_EXPORT_TABLES: tuple[str, ...] = (
    "documents",
    "uploaded_resumes",
    "experience_optimized_docs",
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

# user_api_keys is exported through this projection only — never the
# ``ciphertext`` column. Listing the provider + last4 lets the user see
# which keys are stored without exposing the secret material.
_API_KEYS_PROJECTION = "provider, last4, created_at, updated_at, rotated_at"

_STORAGE_BUCKETS: tuple[str, ...] = (
    resume_storage.STORAGE_BUCKET,
    tailored_storage.STORAGE_BUCKET,
)


def _select_all(
    supabase: Client, table: str, column: str, value: str, columns: str = "*"
) -> list[dict[str, Any]]:
    resp = supabase.table(table).select(columns).eq(column, value).execute()
    return cast(list[dict[str, Any]], resp.data or [])


def collect_user_data(supabase: Client, *, user_id: str) -> dict[str, list[dict[str, Any]]]:
    """Gather every per-user DB row for ``user_id`` into a JSON-able map."""
    data: dict[str, list[dict[str, Any]]] = {}
    for table in _EXPORT_TABLES:
        projection = _API_KEYS_PROJECTION if table == "user_api_keys" else "*"
        data[table] = _select_all(supabase, table, "user_id", user_id, projection)

    profile_rows = _select_all(supabase, "user_profiles", "user_id", user_id)
    data["user_profiles"] = profile_rows

    # notifications_sent is keyed by user_profiles.id, not the auth uid.
    profile_id = str(profile_rows[0]["id"]) if profile_rows else None
    if profile_id is not None:
        data["notifications_sent"] = _select_all(
            supabase, "notifications_sent", "user_profile_id", profile_id
        )
    return data


def _add_storage_files(zf: zipfile.ZipFile, supabase: Client, user_id: str) -> int:
    """Add every object under ``{user_id}/`` in both buckets to the zip.

    A failing bucket is logged and skipped rather than aborting the whole
    export — a partial export still beats no export.
    """
    count = 0
    for bucket_name in _STORAGE_BUCKETS:
        try:
            bucket = supabase.storage.from_(bucket_name)
            listing = bucket.list(user_id) or []
            for obj in listing:
                name = obj["name"]
                blob = bucket.download(f"{user_id}/{name}")
                zf.writestr(f"files/{bucket_name}/{name}", blob)
                count += 1
        except Exception:
            logger.exception("data_export: bucket %s failed for user=%s", bucket_name, user_id)
    return count


def _readme(
    user_id: str,
    generated_at: datetime,
    data: dict[str, list[dict[str, Any]]],
    file_count: int,
) -> str:
    lines = [
        "WyrdFold — personal data export",
        f"User: {user_id}",
        f"Generated: {generated_at.isoformat()}",
        "",
        "data.json holds every database row associated with your account,",
        "grouped by table. Stored API keys are listed without the secret",
        "(provider + last 4 only); derived vector embeddings are omitted.",
        "files/ holds your uploaded resumes and generated documents.",
        "",
        "Rows per table:",
    ]
    lines += [f"  {table}: {len(rows)}" for table, rows in sorted(data.items())]
    lines += ["", f"Files: {file_count}", ""]
    return "\n".join(lines)


def build_export_zip(supabase: Client, *, user_id: str) -> bytes:
    """Build the personal-data export ZIP for ``user_id``; return its bytes."""
    generated_at = datetime.now(UTC)
    data = collect_user_data(supabase, user_id=user_id)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("data.json", json.dumps(data, indent=2, default=str))
        file_count = _add_storage_files(zf, supabase, user_id)
        zf.writestr("README.txt", _readme(user_id, generated_at, data, file_count))

    logger.info(
        "data_export user=%s tables=%d files=%d",
        user_id,
        len(data),
        file_count,
    )
    buf.seek(0)
    return buf.getvalue()
