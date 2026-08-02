"""Preferences CRUD. One row per user_id (unique).

Persistent style bias the LLM reads on every tailoring.
Reset by deleting the row — this is also what re-running onboarding does.
"""

from datetime import UTC, datetime
from typing import Any, cast

from supabase import AsyncClient, Client

from app.constants import resolve_owner
from app.models.experience import Preferences, PreferencesPayload

TABLE = "experience_preferences"


def get(supabase: Client, user_id: str | None) -> Preferences | None:
    query = supabase.table(TABLE).select("*").limit(1)
    query = query.eq("user_id", resolve_owner(user_id))
    resp = query.execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        return None
    return Preferences.model_validate(rows[0])


async def upsert(
    supabase: AsyncClient,
    user_id: str | None,
    payload: PreferencesPayload,
) -> Preferences:
    # Inline the existing-row probe on the async client: the sync ``get`` above
    # stays for its not-yet-converted caller (tailor), so this async path can't
    # reuse it (#57 slice 3).
    existing_resp = await (
        supabase.table(TABLE).select("*").limit(1).eq("user_id", resolve_owner(user_id)).execute()
    )
    existing_rows = cast(list[dict[str, Any]], existing_resp.data or [])
    existing = Preferences.model_validate(existing_rows[0]) if existing_rows else None
    now_iso = datetime.now(UTC).isoformat()
    row: dict[str, Any] = {
        "payload": payload.model_dump(mode="json"),
        "updated_at": now_iso,
    }
    if existing is None:
        row["user_id"] = user_id
        resp = await supabase.table(TABLE).insert(row).execute()
    else:
        resp = await supabase.table(TABLE).update(row).eq("id", existing.id).execute()
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        raise RuntimeError("Failed to upsert preferences")
    return Preferences.model_validate(rows[0])


async def reset(supabase: AsyncClient, user_id: str | None) -> None:
    await supabase.table(TABLE).delete().eq("user_id", resolve_owner(user_id)).execute()
