"""Personal-data export / portability (#29 P2).

Pins the contract:

* ``data.json`` carries every per-user table (lockstep with the deletion
  inventory), with the seeded rows;
* stored API keys are exported WITHOUT the ciphertext;
* ``notifications_sent`` is keyed by the resolved ``user_profiles.id``;
* both Storage buckets' ``{user_id}/`` objects land under ``files/``;
* the endpoint is JWT-gated and streams a valid zip.
"""

from __future__ import annotations

import io
import json
import zipfile
from types import SimpleNamespace
from typing import Any

from app.services import account_deletion
from app.services.data_export import _EXPORT_TABLES, build_export_zip

_UID = "u1"
_PROFILE_ID = "profile-1"
_SECRET = "CIPHERTEXT-SHOULD-NOT-LEAK"


class _FakeQuery:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self._cols = "*"
        self._filters: list[tuple[str, Any]] = []
        self._in_filters: list[tuple[str, list[Any]]] = []

    def select(self, cols: str) -> _FakeQuery:
        self._cols = cols
        return self

    def eq(self, col: str, val: Any) -> _FakeQuery:
        self._filters.append((col, val))
        return self

    def in_(self, col: str, vals: list[Any]) -> _FakeQuery:
        self._in_filters.append((col, list(vals)))
        return self

    def execute(self) -> SimpleNamespace:
        matched = [
            r
            for r in self._rows
            if all(r.get(c) == v for c, v in self._filters)
            and all(r.get(c) in vals for c, vals in self._in_filters)
        ]
        if self._cols != "*":
            keep = [c.strip() for c in self._cols.split(",")]
            matched = [{k: r.get(k) for k in keep} for r in matched]
        return SimpleNamespace(data=matched)


class _FakeBucket:
    """Mimics storage3: ``list`` returns at most ``limit`` (default 100)
    objects starting at ``offset`` — the same paging contract the real
    client enforces, so the export's pagination loop is exercised."""

    DEFAULT_LIMIT = 100

    def __init__(self, objects: dict[str, dict[str, bytes]]) -> None:
        self._objects = objects

    def list(self, prefix: str, options: dict[str, Any] | None = None) -> list[dict[str, str]]:
        names = sorted(self._objects.get(prefix, {}))
        opts = options or {}
        limit = opts.get("limit", self.DEFAULT_LIMIT)
        offset = opts.get("offset", 0)
        return [{"name": n} for n in names[offset : offset + limit]]

    def download(self, path: str) -> bytes:
        prefix, name = path.split("/", 1)
        return self._objects.get(prefix, {}).get(name, b"")


class _FakeStorage:
    def __init__(self, buckets: dict[str, dict[str, dict[str, bytes]]]) -> None:
        self._buckets = buckets

    def from_(self, name: str) -> _FakeBucket:
        return _FakeBucket(self._buckets.get(name, {}))


class _FakeSupabase:
    def __init__(
        self,
        tables: dict[str, list[dict[str, Any]]],
        buckets: dict[str, dict[str, dict[str, bytes]]] | None = None,
    ) -> None:
        self.tables = tables
        self.storage = _FakeStorage(buckets or {})

    def table(self, name: str) -> _FakeQuery:
        return _FakeQuery(self.tables.get(name, []))


def _seeded() -> _FakeSupabase:
    tables: dict[str, list[dict[str, Any]]] = {
        "user_profiles": [{"id": _PROFILE_ID, "user_id": _UID, "email": "j@example.com"}],
        "experience_prose_docs": [{"user_id": _UID, "prose": "I led teams."}],
        "job_feedback": [{"user_id": _UID, "reason": "too junior"}],
        "user_jobs": [{"user_id": _UID, "job_posting_id": "j1", "status": "applied"}],
        "user_api_keys": [
            {
                "user_id": _UID,
                "provider": "openrouter",
                "last4": "ab12",
                "ciphertext": _SECRET,
                "created_at": "2026-06-01T00:00:00+00:00",
                "updated_at": "2026-06-01T00:00:00+00:00",
                "rotated_at": None,
            }
        ],
        "notifications_sent": [{"user_profile_id": _PROFILE_ID, "channel": "email"}],
    }
    buckets = {
        "resume-uploads": {_UID: {"original.pdf": b"PDF-BYTES"}},
        "tailored-resumes": {_UID: {"resume-1.docx": b"DOCX-BYTES"}},
    }
    return _FakeSupabase(tables, buckets)


def _open(blob: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(blob))


def _data_json(blob: bytes) -> dict[str, Any]:
    return json.loads(_open(blob).read("data.json"))


def test_export_inventory_in_lockstep_with_deletion() -> None:
    """Export and erasure must cover the same per-user tables — those deleted
    on erasure plus those anonymized (the user's shared contributions)."""
    assert set(_EXPORT_TABLES) == (
        set(account_deletion._USER_ID_TABLES)
        | set(account_deletion._ANONYMIZED_TABLES)
    )


def test_data_json_covers_all_tables_with_rows() -> None:
    data = _data_json(build_export_zip(_seeded(), user_id=_UID))
    for table in _EXPORT_TABLES:
        assert table in data, table
    assert data["user_profiles"][0]["email"] == "j@example.com"
    assert data["experience_prose_docs"][0]["prose"] == "I led teams."
    assert data["job_feedback"][0]["reason"] == "too junior"


def test_scores_pii_exported_for_user_targets_only() -> None:
    """The shared ``scores`` catalog has no user_id; the export must include
    the Phase-2 grader PII for the user's own targets and nothing else."""
    sb = _FakeSupabase(
        {
            "user_profiles": [{"id": _PROFILE_ID, "user_id": _UID}],
            "user_targets": [{"user_id": _UID, "target_id": "t1"}],
            "scores": [
                {
                    "target_id": "t1",
                    "job_posting_id": "j1",
                    "score": 80,
                    "fit_reasoning": "Your FightCamp work (Lighthouse +40)",
                    "axis_scores": {"skills_fit": 90},
                    "logistics_filters": {"remote": True},
                    "scoring_status": "complete",
                    "updated_at": "2026-06-01T00:00:00+00:00",
                },
                {"target_id": "t2", "job_posting_id": "j1", "fit_reasoning": "not yours"},
            ],
        }
    )
    data = _data_json(build_export_zip(sb, user_id=_UID))
    assert len(data["scores"]) == 1
    row = data["scores"][0]
    assert row["target_id"] == "t1"
    assert row["fit_reasoning"] == "Your FightCamp work (Lighthouse +40)"
    assert row["axis_scores"] == {"skills_fit": 90}


def test_scores_absent_when_user_has_no_targets() -> None:
    data = _data_json(build_export_zip(_seeded(), user_id=_UID))
    assert data["scores"] == []


def test_api_keys_exported_without_ciphertext() -> None:
    blob = build_export_zip(_seeded(), user_id=_UID)
    key_row = _data_json(blob)["user_api_keys"][0]
    assert key_row["provider"] == "openrouter"
    assert key_row["last4"] == "ab12"
    assert "ciphertext" not in key_row
    # Belt-and-suspenders: the secret appears nowhere in the bundle.
    assert _SECRET.encode() not in blob


def test_notifications_keyed_by_resolved_profile_id() -> None:
    data = _data_json(build_export_zip(_seeded(), user_id=_UID))
    assert data["notifications_sent"][0]["channel"] == "email"


def test_storage_files_bundled_from_both_buckets() -> None:
    zf = _open(build_export_zip(_seeded(), user_id=_UID))
    names = set(zf.namelist())
    assert "files/resume-uploads/original.pdf" in names
    assert "files/tailored-resumes/resume-1.docx" in names
    assert zf.read("files/resume-uploads/original.pdf") == b"PDF-BYTES"
    assert "README.txt" in names


def test_no_profile_skips_notifications_without_crashing() -> None:
    sb = _FakeSupabase({"experience_prose_docs": [{"user_id": _UID, "prose": "x"}]})
    data = _data_json(build_export_zip(sb, user_id=_UID))
    assert "notifications_sent" not in data
    assert data["user_profiles"] == []


def test_storage_export_pages_past_one_page() -> None:
    """A user with >1 page of files must get ALL of them bundled.

    Regression: ``bucket.list`` caps a page at 100 objects, so a single
    un-paged call truncated the export to 100 while account deletion (which
    walks pages) still removed everything — breaking the lockstep promise.
    """
    many = {f"resume-{i:03d}.pdf": f"BYTES-{i}".encode() for i in range(150)}
    sb = _FakeSupabase(
        {"user_profiles": [{"id": _PROFILE_ID, "user_id": _UID}]},
        {"resume-uploads": {_UID: many}, "tailored-resumes": {}},
    )
    zf = _open(build_export_zip(sb, user_id=_UID))
    bundled = [n for n in zf.namelist() if n.startswith("files/resume-uploads/")]
    assert len(bundled) == 150
    # Every distinct object made it (no overwrite/dupe from offset reuse).
    assert len(set(bundled)) == 150
    assert zf.read("files/resume-uploads/resume-149.pdf") == b"BYTES-149"


def test_readme_file_count_matches_bundled_files() -> None:
    """The README manifest must not under-report when paging kicks in."""
    many = {f"r-{i:03d}.pdf": f"B{i}".encode() for i in range(150)}
    sb = _FakeSupabase(
        {"user_profiles": [{"id": _PROFILE_ID, "user_id": _UID}]},
        {"resume-uploads": {_UID: many}, "tailored-resumes": {}},
    )
    zf = _open(build_export_zip(sb, user_id=_UID))
    bundled = len([n for n in zf.namelist() if n.startswith("files/")])
    readme = zf.read("README.txt").decode()
    files_line = next(ln for ln in readme.splitlines() if ln.startswith("Files:"))
    assert int(files_line.split(":", 1)[1].strip()) == bundled == 150


def test_same_filename_in_both_buckets_does_not_collide() -> None:
    """Identical object names in each bucket both survive (namespaced by
    ``files/<bucket>/``)."""
    sb = _FakeSupabase(
        {"user_profiles": [{"id": _PROFILE_ID, "user_id": _UID}]},
        {
            "resume-uploads": {_UID: {"doc.pdf": b"FROM-UPLOADS"}},
            "tailored-resumes": {_UID: {"doc.pdf": b"FROM-TAILORED"}},
        },
    )
    zf = _open(build_export_zip(sb, user_id=_UID))
    assert zf.read("files/resume-uploads/doc.pdf") == b"FROM-UPLOADS"
    assert zf.read("files/tailored-resumes/doc.pdf") == b"FROM-TAILORED"


def test_export_endpoint_is_jwt_gated_and_streams_zip() -> None:
    from fastapi.testclient import TestClient

    from app.dependencies import get_current_user_id, get_supabase, verify_supabase_jwt
    from app.main import app

    sb = _seeded()
    app.dependency_overrides[verify_supabase_jwt] = lambda: _UID
    app.dependency_overrides[get_current_user_id] = lambda: _UID
    app.dependency_overrides[get_supabase] = lambda: sb
    try:
        client = TestClient(app)
        resp = client.get("/profile/export")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert "attachment" in resp.headers["content-disposition"]
    data = _data_json(resp.content)
    assert data["user_profiles"][0]["user_id"] == _UID
