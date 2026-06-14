"""DOCX storage round-trip contract (#28 follow-up).

`upload_docx` and `download_docx` construct their Supabase Storage path
independently — upload via `_storage_path(user_id, resume_id)`, download
from whatever path string the caller persisted. Existing tests mock at
the `storage.from_` boundary and only assert the bucket name, so a
divergence between the two (e.g. someone changes the path format in one
place) would slip through.

These tests use an in-memory storage fake that actually keys bytes by
(bucket, path), so the round-trip only passes if `download_docx` reads
back the exact path `upload_docx` wrote — and the user-namespacing that
keeps one tenant's artifacts out of another's folder is pinned too.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services.tailor import persistence


class _FakeBucket:
    """Records uploads keyed by path; serves them back on download.

    Mirrors the supabase-py storage surface the persistence layer uses:
    `.upload(path=, file=, file_options=)` and `.download(path)`.
    """

    def __init__(self, store: dict[str, bytes]) -> None:
        self._store = store

    def upload(self, *, path: str, file: bytes, file_options: dict) -> None:
        self._store[path] = file

    def download(self, path: str) -> bytes:
        if path not in self._store:
            # The real client raises on a missing object; mirror that so a
            # path mismatch surfaces as an error rather than silent None.
            raise KeyError(f"no object at {path}")
        return self._store[path]


class _FakeStorage:
    def __init__(self) -> None:
        # One dict per bucket → {path: bytes}
        self.buckets: dict[str, dict[str, bytes]] = {}

    def from_(self, bucket: str) -> _FakeBucket:
        return _FakeBucket(self.buckets.setdefault(bucket, {}))


def _supabase_with_fake_storage() -> MagicMock:
    sb = MagicMock()
    sb.storage = _FakeStorage()
    return sb


def test_upload_then_download_returns_same_bytes() -> None:
    """The path upload returns must be the path download reads."""
    sb = _supabase_with_fake_storage()
    payload = b"PK\x03\x04 fake docx bytes"

    path = persistence.upload_docx(
        sb, user_id="user-1", resume_id="resume-1", docx_bytes=payload
    )
    got = persistence.download_docx(sb, path)

    assert got == payload


def test_upload_path_is_user_namespaced() -> None:
    """Artifacts land under the owner's folder — the isolation contract
    that keeps one user's .docx out of another's prefix."""
    sb = _supabase_with_fake_storage()

    path = persistence.upload_docx(
        sb, user_id="user-abc", resume_id="r-9", docx_bytes=b"x"
    )

    assert path == "user-abc/r-9.docx"
    assert path in sb.storage.buckets[persistence.STORAGE_BUCKET]


def test_storage_path_is_namespaced_under_user_id() -> None:
    """Every object is filed under the owner's `<user_id>/` folder — the
    legacy `anon/` fallback is gone, and storage RLS keys on this prefix."""
    sb = _supabase_with_fake_storage()

    path = persistence.upload_docx(
        sb, user_id="user-1", resume_id="r-1", docx_bytes=b"y"
    )

    assert path == "user-1/r-1.docx"


def test_two_users_same_resume_id_do_not_collide() -> None:
    """Identical resume_id under different users must produce distinct
    objects — a regression here would let one tenant overwrite/serve
    another's artifact."""
    sb = _supabase_with_fake_storage()

    p1 = persistence.upload_docx(
        sb, user_id="user-1", resume_id="shared-id", docx_bytes=b"one"
    )
    p2 = persistence.upload_docx(
        sb, user_id="user-2", resume_id="shared-id", docx_bytes=b"two"
    )

    assert p1 != p2
    assert persistence.download_docx(sb, p1) == b"one"
    assert persistence.download_docx(sb, p2) == b"two"


def test_download_unknown_path_raises() -> None:
    """A path that was never uploaded surfaces as an error, not silent
    empty bytes — the download endpoint depends on this to 404/502."""
    sb = _supabase_with_fake_storage()

    with pytest.raises(KeyError):
        persistence.download_docx(sb, "user-1/never-written.docx")


def test_upload_sets_docx_content_type() -> None:
    """The content-type must be the DOCX MIME so browsers download
    rather than render — pinned via a spy on the bucket upload call."""
    sb = MagicMock()
    bucket = MagicMock()
    sb.storage.from_.return_value = bucket

    persistence.upload_docx(
        sb, user_id="u", resume_id="r", docx_bytes=b"z"
    )

    _, kwargs = bucket.upload.call_args
    assert kwargs["file_options"]["content-type"] == persistence.DOCX_CONTENT_TYPE
    # upsert keeps re-renders idempotent (same path overwrites).
    assert kwargs["file_options"]["upsert"] == "true"
