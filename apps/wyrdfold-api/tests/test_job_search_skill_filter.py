"""Skill filter on the catalog search (skill-search feature).

Pins the three things that would silently break the facet:

- read/write vocabulary agreement — the DB predicate is exact-string jsonb
  containment, so "React" from a caller MUST become the stored "react";
- AND semantics across multiple skills, applied DB-side;
- cache-key isolation — the search response cache is keyed by query shape,
  so a skill-filtered request must NOT be served an unfiltered cached page
  (the bug this would cause is invisible in a service-level test).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import job_search


def _capturing_supabase(rows: list[dict[str, Any]] | None = None) -> tuple[MagicMock, dict]:
    """Supabase mock recording the ``.filter()`` calls the skill filter makes.

    Records the RENDERED operator + value, not a Python list, because the
    encoding is where this broke: ``.contains(col, ["react"])`` sends a Postgres
    array literal (``cs.{react}``), which a JSONB column rejects with 22P02 —
    the app-wide handler turns that into a 404, i.e. a facet that silently
    returns nothing. A mock asserting "contains was called with the right list"
    passed while the real query 404'd; asserting the JSON-encoded string is what
    ties this test to PostgREST's actual contract.
    """
    captured: dict[str, Any] = {"filters": []}
    tbl = MagicMock()
    for method in ("select", "is_", "or_", "eq", "gte", "order", "limit", "contains"):
        getattr(tbl, method).return_value = tbl
    tbl.not_.is_.return_value = tbl

    def _filter(col: str, op: str, val: object) -> MagicMock:
        captured["filters"].append((col, op, val))
        return tbl

    tbl.filter.side_effect = _filter
    resp = MagicMock()
    resp.data = rows or []
    tbl.execute = AsyncMock(return_value=resp)
    sb = MagicMock()
    sb.table.return_value = tbl
    return sb, captured


# ---- normalization -----------------------------------------------------------


def test_normalize_skill_filter_matches_the_write_vocabulary() -> None:
    """Caller casing/spacing must collapse to the stored form, dedupe, and cap.

    Shares ``normalize_skill`` with the tagger + harvest write paths — this
    test is the contract that keeps read and write from drifting apart.
    """
    assert job_search.normalize_skill_filter(["React"]) == ["react"]
    assert job_search.normalize_skill_filter(["  Node.JS  "]) == ["node.js"]
    # Duplicates after normalization collapse; order of first appearance holds.
    assert job_search.normalize_skill_filter(["React", "react", "SQL"]) == ["react", "sql"]
    # Junk drops rather than reaching the DB predicate.
    assert job_search.normalize_skill_filter(["", "   "]) == []
    assert job_search.normalize_skill_filter(None) == []
    assert job_search.normalize_skill_filter([]) == []
    # Sentence-length input is not a skill.
    assert job_search.normalize_skill_filter(["x" * 61]) == []
    # Capped.
    many = [f"skill{i}" for i in range(job_search.MAX_SKILL_FILTER_TERMS + 4)]
    assert len(job_search.normalize_skill_filter(many)) == job_search.MAX_SKILL_FILTER_TERMS


# ---- service-level filter ----------------------------------------------------


@pytest.mark.asyncio
async def test_skill_filter_applies_json_encoded_containment_db_side() -> None:
    sb, captured = _capturing_supabase()

    await job_search.search_jobs(sb, q="frontend", skills=["React", "Node.JS"])

    # ONE `cs` filter carrying BOTH normalized skills as a JSON array string.
    # jsonb @> with a multi-element array is AND — the intended narrow — and the
    # JSON encoding is what makes PostgREST accept it against a JSONB column
    # (a Python list renders as `{react}` and 22P02s; see the fixture docstring).
    assert captured["filters"] == [("skills_required", "cs", '["react", "node.js"]')]


@pytest.mark.asyncio
async def test_no_skills_means_no_containment_filter() -> None:
    """The filter must be entirely absent when unused — not an empty-array
    containment, which would match only rows with an empty list."""
    sb, captured = _capturing_supabase()

    await job_search.search_jobs(sb, q="frontend")
    await job_search.search_jobs(sb, q="frontend", skills=[])
    await job_search.search_jobs(sb, q="frontend", skills=["   "])

    assert captured["filters"] == []


# ---- cache-key isolation (the invisible bug) ---------------------------------


def test_cache_key_varies_with_the_skill_filter() -> None:
    """A skill-filtered request must not collide with the unfiltered one.

    Without the skill term in the key, `?q=frontend` warms the cache and
    `?q=frontend&skill=react` is served those unfiltered rows — a wrong
    result set with a 200, invisible to any service-level assertion.
    """
    from app.cache import make_cache_key

    def key(skills: list[str] | None) -> str:
        return make_cache_key(
            "search",
            q="frontend",
            page_size=20,
            offset=0,
            location="",
            posted_within_days=0,
            salary_floor=0,
            skills=",".join(job_search.normalize_skill_filter(skills)),
        )

    unfiltered = key(None)
    react = key(["react"])
    react_node = key(["react", "node.js"])

    assert len({unfiltered, react, react_node}) == 3
    # Equivalent spellings share a key (cache stays effective across casings).
    assert key(["React"]) == react
