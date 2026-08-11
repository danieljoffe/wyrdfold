"""Supabase Storage for uploaded resume files.

Follows the same pattern as tailor/persistence.py. Stores originals
so users can reference what they uploaded.
"""

from __future__ import annotations

from supabase import AsyncClient, Client

STORAGE_BUCKET = "resume-uploads"


def _storage_path(user_id: str, upload_id: str, file_ext: str) -> str:
    return f"{user_id}/{upload_id}.{file_ext}"


async def upload_file(
    supabase: AsyncClient,
    *,
    user_id: str,
    upload_id: str,
    file_bytes: bytes,
    file_ext: str,
    content_type: str,
) -> str:
    """Upload a resume file to Supabase Storage. Returns the storage path.

    ``supabase`` must be the async service/user client and ``user_id`` the
    caller's id: storage RLS keys access on the ``<user_id>/`` path prefix,
    so the object lands in (and is readable from) only the owner's folder.
    Async on the pooled client (#57 slice 3)."""
    path = _storage_path(user_id, upload_id, file_ext)
    await supabase.storage.from_(STORAGE_BUCKET).upload(
        path=path,
        file=file_bytes,
        file_options={"content-type": content_type, "upsert": "true"},
    )
    return path


def download_file(supabase: Client, storage_path: str) -> bytes:
    """Download a resume file from Supabase Storage."""
    return supabase.storage.from_(STORAGE_BUCKET).download(storage_path)


async def purge_user_objects(supabase: AsyncClient, user_id: str) -> int:
    """Delete every object under the user's ``<user_id>/`` prefix.

    Returns the number of objects removed. Used by account deletion
    (#29). Loops list→remove until the prefix is empty so it covers more
    than one storage page; bounded to avoid an unbounded loop if a
    backend ever fails to remove. Paths are flat (``<user_id>/<file>``),
    so a single-level listing is sufficient. Async on the pooled client
    (#57 slice 3)."""
    bucket = supabase.storage.from_(STORAGE_BUCKET)
    removed = 0
    for _ in range(1000):  # safety bound: 1000 pages
        listing = await bucket.list(user_id) or []
        names = [obj["name"] for obj in listing if obj.get("name")]
        if not names:
            break
        await bucket.remove([f"{user_id}/{name}" for name in names])
        removed += len(names)
    return removed
