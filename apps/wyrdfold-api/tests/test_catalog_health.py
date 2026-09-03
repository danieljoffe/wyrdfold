"""Catalog-health observability (#958) — metrics, tripwire, recorder, endpoint.

The tripwire tests are the point of the feature: the synthetic regime change
(engineer-vocabulary baseline → assistant-vocabulary window, #952's exact
signature) MUST fire, and the test fails if the guard is removed or made
vacuous. The refusal tests pin the other half of the contract: below the
sample floors the tripwire says "not evaluated", never a verdict.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.dependencies import get_async_service_supabase, verify_api_key
from app.main import app
from app.services import catalog_health
from app.services.catalog_health import (
    _median_age_hours,
    evaluate_tripwire,
    record_cycle_health,
    tokenize_titles,
    tv_distance,
)

# ---- fakes ------------------------------------------------------------------


class _FakeQuery:
    """Self-returning chainable builder; ``execute`` pops the owner's next
    queued result for its table. ``insert``/``delete`` payloads and every
    chained (method, args) pair are captured so tests can assert what was
    written and which filters were applied. A queued item shaped
    ``{"data": ..., "count": N}`` feeds an exact-count response."""

    def __init__(self, owner: _FakeSupabase, table: str) -> None:
        self._owner = owner
        self._table = table

    @property
    def not_(self) -> _FakeQuery:
        self._owner.calls.setdefault(self._table, []).append(("not_", ()))
        return self

    def __getattr__(self, name: str) -> Any:
        def _chain(*args: Any, **kwargs: Any) -> _FakeQuery:
            self._owner.calls.setdefault(self._table, []).append((name, args))
            if name == "insert":
                self._owner.inserted.setdefault(self._table, []).append(args[0])
            if name == "delete":
                self._owner.deleted.append(self._table)
            return self

        return _chain

    async def execute(self) -> SimpleNamespace:
        item = self._owner.next_result(self._table)
        if isinstance(item, dict) and "data" in item:
            return SimpleNamespace(data=item["data"], count=item.get("count"))
        return SimpleNamespace(data=item, count=None)


class _FakeSupabase:
    """Queue-per-table fake: ``results[table]`` is consumed in call order,
    covering the recorder's fixed query sequence (throttle → jobs count →
    jobs pages → baseline → scores chunks → rpc → insert → delete)."""

    def __init__(self, results: dict[str, list[Any]], rpc_data: Any = None) -> None:
        self.results = {k: list(v) for k, v in results.items()}
        self.rpc_data = rpc_data
        self.inserted: dict[str, list[dict[str, Any]]] = {}
        self.deleted: list[str] = []
        self.calls: dict[str, list[tuple[str, tuple[Any, ...]]]] = {}

    def table(self, name: str) -> _FakeQuery:
        return _FakeQuery(self, name)

    def rpc(self, name: str, params: dict[str, Any]) -> _FakeQuery:
        q = _FakeQuery(self, f"rpc:{name}")
        self.results.setdefault(f"rpc:{name}", []).append(self.rpc_data)
        return q

    def next_result(self, table: str) -> Any:
        queue = self.results.get(table) or []
        return queue.pop(0) if queue else []


def _job(jid: str, title: str, *, hours_ago: int = 1, posted_hours_ago: int | None = 5) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "id": jid,
        "title": title,
        "cataloged_at": (now - timedelta(hours=hours_ago)).isoformat(),
        "source_posted_at": (
            (now - timedelta(hours=posted_hours_ago)).isoformat()
            if posted_hours_ago is not None
            else None
        ),
    }


# ---- tokenizer + distance ---------------------------------------------------


def test_tokenize_titles_counts_role_vocabulary() -> None:
    counts = tokenize_titles(
        ["Sr. Frontend Engineer!", "frontend engineer", "Engineer, Backend (1498)"]
    )
    # Sanitized + counted across titles; per-title dedupe comes from the
    # search tokenizer, so "frontend engineer" contributes each token once.
    assert counts["frontend"] == 2
    assert counts["engineer"] == 3
    assert counts["backend"] == 1
    # Short and numeric tokens are noise, not vocabulary.
    assert "sr" not in counts
    assert "1498" not in counts


def test_tv_distance_bounds() -> None:
    same = Counter({"engineer": 10, "frontend": 5})
    assert tv_distance(same, Counter(same)) == pytest.approx(0.0)
    disjoint_a = Counter({"engineer": 10})
    disjoint_b = Counter({"assistant": 10})
    assert tv_distance(disjoint_a, disjoint_b) == pytest.approx(1.0)


# ---- tripwire ---------------------------------------------------------------

_ENGINEER_BASELINE = Counter(
    {"engineer": 40, "frontend": 15, "backend": 12, "senior": 10, "developer": 8}
)
_ASSISTANT_WINDOW = Counter(
    {"assistant": 18, "specialist": 12, "representative": 9, "coordinator": 6}
)


def test_tripwire_fires_on_a_regime_change() -> None:
    # The #952 signature: engineer-vocabulary baseline, assistant-vocabulary
    # window. This is the sabotage guard for the whole feature — neutering
    # evaluate_tripwire (always-quiet, inverted threshold, dropped distance)
    # fails here.
    fired, distance, reason = evaluate_tripwire(
        _ASSISTANT_WINDOW,
        _ENGINEER_BASELINE,
        threshold=0.5,
        min_titles=20,
        current_titles=45,
        baseline_titles=100,
    )
    assert fired is True
    assert distance is not None and distance > 0.5
    assert reason is not None and "shifted" in reason


def test_tripwire_quiet_on_a_stable_mix() -> None:
    window = Counter({t: max(1, c // 2) for t, c in _ENGINEER_BASELINE.items()})
    fired, distance, reason = evaluate_tripwire(
        window,
        _ENGINEER_BASELINE,
        threshold=0.5,
        min_titles=20,
        current_titles=45,
        baseline_titles=100,
    )
    assert fired is False
    assert distance is not None and distance < 0.1
    assert reason is None


def test_tripwire_floor_counts_titles_not_tokens() -> None:
    # Ten ordinary titles yield ~30 token occurrences — under a token-based
    # floor of 20 they'd impersonate a big sample. The floor must count
    # TITLES: 10 titles < 20 refuses, whatever the token totals say.
    busy_tokens = Counter({"assistant": 12, "specialist": 10, "coordinator": 8})
    fired, distance, reason = evaluate_tripwire(
        busy_tokens,
        _ENGINEER_BASELINE,
        threshold=0.5,
        min_titles=20,
        current_titles=10,
        baseline_titles=100,
    )
    assert (fired, distance) == (False, None)
    assert reason is not None and "10 titles < 20" in reason


def test_tripwire_refuses_a_small_window() -> None:
    fired, distance, reason = evaluate_tripwire(
        Counter({"assistant": 3}),
        _ENGINEER_BASELINE,
        threshold=0.5,
        min_titles=20,
        current_titles=3,
        baseline_titles=100,
    )
    assert (fired, distance) == (False, None)
    assert reason is not None and "window sample too small" in reason


def test_tripwire_refuses_a_small_baseline() -> None:
    fired, distance, reason = evaluate_tripwire(
        _ASSISTANT_WINDOW,
        Counter({"engineer": 2}),
        threshold=0.5,
        min_titles=20,
        current_titles=45,
        baseline_titles=2,
    )
    assert (fired, distance) == (False, None)
    assert reason is not None and "baseline too small" in reason


# ---- median age -------------------------------------------------------------


def test_median_age_hours() -> None:
    rows = [
        _job("a", "x", hours_ago=1, posted_hours_ago=5),  # 4h old at admission
        _job("b", "x", hours_ago=1, posted_hours_ago=11),  # 10h
        _job("c", "x", hours_ago=1, posted_hours_ago=None),  # undatable — excluded
    ]
    assert _median_age_hours(rows) == pytest.approx(7.0, abs=0.2)
    assert _median_age_hours([_job("c", "x", posted_hours_ago=None)]) is None


# ---- recorder ---------------------------------------------------------------


@pytest.fixture
def small_samples(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "catalog_health_min_sample_titles", 4)
    monkeypatch.setattr(settings, "catalog_health_min_interval_minutes", 30)


async def test_record_cycle_health_writes_the_row(small_samples: None) -> None:
    window = [
        _job("j1", "Frontend Engineer"),
        _job("j2", "Backend Engineer"),
        _job("j3", "Product Designer"),
        _job("j4", "Data Engineer"),
    ]
    sb = _FakeSupabase(
        {
            "catalog_health_cycles": [
                [],  # throttle check: no prior row
                [],  # baseline read: empty (young table)
                [],  # insert ack
                [],  # prune ack
            ],
            "jobs": [{"data": [], "count": 4}, window],
            "scores": [[{"job_posting_id": "j1"}, {"job_posting_id": "j2"}]],
        },
        rpc_data={
            "live_total": 200,
            "ungraded": 50,
            "location_unknown": 20,
            "family_counts": {"frontend": 120, "untagged": 80},
        },
    )

    row = await record_cycle_health(sb)  # type: ignore[arg-type]

    assert row is not None
    inserted = sb.inserted["catalog_health_cycles"][0]
    assert inserted["new_jobs"] == 4
    assert inserted["window_truncated"] is False
    assert inserted["relevant_jobs"] == 2
    # The window query applies the SAME live-US corpus gate as the snapshot
    # RPC, so the metrics beside each other describe the same catalog.
    assert ("is_", ("is_us", "false")) in sb.calls["jobs"]
    assert ("not_", ()) in sb.calls["jobs"]
    assert inserted["live_total"] == 200
    assert inserted["pct_ungraded"] == 25.0
    assert inserted["pct_location_unknown"] == 10.0
    assert inserted["family_counts"] == {"frontend": 120, "untagged": 80}
    tokens = dict(inserted["top_title_tokens"])
    assert tokens["engineer"] == 3
    # Empty baseline → the tripwire refuses rather than guessing.
    assert inserted["tripwire_fired"] is False
    assert inserted["tripwire_reason"] is not None
    assert "baseline too small" in inserted["tripwire_reason"]
    # Retention pruning ran.
    assert "catalog_health_cycles" in sb.deleted


async def test_record_cycle_health_fires_against_a_shifted_baseline(
    small_samples: None,
) -> None:
    window = [
        _job("j1", "Executive Assistant"),
        _job("j2", "Sales Specialist"),
        _job("j3", "Support Representative"),
        _job("j4", "Office Coordinator"),
    ]
    baseline_rows = [
        {
            "top_title_tokens": [["engineer", 40], ["frontend", 15], ["backend", 12]],
            "new_jobs": 40,
        }
    ]
    sb = _FakeSupabase(
        {
            "catalog_health_cycles": [[], baseline_rows, [], []],
            "jobs": [{"data": [], "count": 4}, window],
            "scores": [[]],
        },
        rpc_data={"live_total": 10, "ungraded": 0, "location_unknown": 0, "family_counts": {}},
    )

    row = await record_cycle_health(sb)  # type: ignore[arg-type]

    assert row is not None
    inserted = sb.inserted["catalog_health_cycles"][0]
    assert inserted["tripwire_fired"] is True
    assert inserted["tripwire_distance"] is not None
    assert inserted["relevant_jobs"] == 0  # nothing entered any pipeline


async def test_recorder_compares_top_n_symmetrically(small_samples: None) -> None:
    # A stable BROAD vocabulary (30 distinct tokens, only 15 persisted) must
    # read as distance ~0: the baseline is reconstructed from truncated top-15
    # histograms, so the current side must be truncated the same way. The
    # pre-fix comparison (full current vs truncated baseline) scores this
    # exact fixture at tv≈0.28 — the false positive that would train the
    # operator to ignore the one loud signal this feature exists to produce.
    head = {f"headword{i:02d}": 20 - i for i in range(15)}  # counts 20..6
    tail = {f"tailword{i:02d}": 5 for i in range(15)}
    window = []
    jid = 0
    for token, count in {**head, **tail}.items():
        for _ in range(count):
            jid += 1
            window.append(_job(f"j{jid}", token))
    baseline_rows = [
        {
            "top_title_tokens": [[t, c] for t, c in sorted(head.items(), key=lambda kv: -kv[1])],
            "new_jobs": len(window),
        }
    ]
    sb = _FakeSupabase(
        {
            "catalog_health_cycles": [[], baseline_rows, [], []],
            "jobs": [{"data": [], "count": len(window)}, window],
            "scores": [[], []],  # two id chunks (270 ids)
        },
        rpc_data={"live_total": 1, "ungraded": 0, "location_unknown": 0, "family_counts": {}},
    )

    row = await record_cycle_health(sb)  # type: ignore[arg-type]

    assert row is not None
    inserted = sb.inserted["catalog_health_cycles"][0]
    assert inserted["tripwire_fired"] is False
    assert inserted["tripwire_distance"] is not None
    assert inserted["tripwire_distance"] <= 0.05


async def test_recorder_baseline_truncates_across_rotating_tails(
    small_samples: None,
) -> None:
    # The second asymmetry from the #974 review: each historical row stores
    # only ITS top-15, but ranks ~10-20 rotate between cycles, so the union
    # across 4 rows spans 45 distinct tokens here. Compared raw, that wide
    # historical support carries mass the current window can't match and a
    # STABLE population reads as a regime shift (this fixture scores
    # tv≈0.55 > 0.5 → false fire). Truncating the aggregate back to top-15
    # scores tv≈0.33 → quiet. The dominant vocabulary never changed.
    dominant = {f"domword{i}": 20 for i in range(5)}
    mid = {f"midword{i}": 5 for i in range(10)}
    window = []
    jid = 0
    for token, count in {**dominant, **mid}.items():
        for _ in range(count):
            jid += 1
            window.append(_job(f"j{jid}", token))
    baseline_rows = [
        {
            "top_title_tokens": (
                [[t, c] for t, c in dominant.items()]
                + [[f"noise{r}x{i}", 12] for i in range(10)]
            ),
            "new_jobs": 140,
        }
        for r in range(4)
    ]
    sb = _FakeSupabase(
        {
            "catalog_health_cycles": [[], baseline_rows, [], []],
            "jobs": [{"data": [], "count": len(window)}, window],
            "scores": [[]],
        },
        rpc_data={"live_total": 1, "ungraded": 0, "location_unknown": 0, "family_counts": {}},
    )

    row = await record_cycle_health(sb)  # type: ignore[arg-type]

    assert row is not None
    inserted = sb.inserted["catalog_health_cycles"][0]
    assert inserted["tripwire_fired"] is False
    assert inserted["tripwire_distance"] is not None
    assert inserted["tripwire_distance"] < 0.45


async def test_recorder_pages_through_a_large_window(
    small_samples: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(catalog_health, "_WINDOW_PAGE", 2)
    monkeypatch.setattr(catalog_health, "_WINDOW_MAX_PAGES", 5)
    jobs = [_job(f"j{i}", "Frontend Engineer") for i in range(5)]
    sb = _FakeSupabase(
        {
            "catalog_health_cycles": [[], [], [], []],
            # count, then three deterministic pages (2 + 2 + 1).
            "jobs": [{"data": [], "count": 5}, jobs[:2], jobs[2:4], jobs[4:]],
            "scores": [[]],
        },
        rpc_data={"live_total": 5, "ungraded": 0, "location_unknown": 0, "family_counts": {}},
    )

    row = await record_cycle_health(sb)  # type: ignore[arg-type]

    assert row is not None
    inserted = sb.inserted["catalog_health_cycles"][0]
    assert inserted["new_jobs"] == 5
    assert inserted["window_truncated"] is False
    # All three pages fed the histogram, not just the first.
    assert dict(inserted["top_title_tokens"])["engineer"] == 5


async def test_recorder_is_honest_about_a_truncated_window(
    small_samples: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(catalog_health, "_WINDOW_PAGE", 2)
    monkeypatch.setattr(catalog_health, "_WINDOW_MAX_PAGES", 2)
    jobs = [_job(f"j{i}", "Frontend Engineer") for i in range(4)]
    sb = _FakeSupabase(
        {
            "catalog_health_cycles": [[], [], [], []],
            # 10 rows exist; the backstop collects only 2 full pages (4 rows).
            "jobs": [{"data": [], "count": 10}, jobs[:2], jobs[2:]],
            "scores": [[]],
        },
        rpc_data={"live_total": 10, "ungraded": 0, "location_unknown": 0, "family_counts": {}},
    )

    row = await record_cycle_health(sb)  # type: ignore[arg-type]

    assert row is not None
    inserted = sb.inserted["catalog_health_cycles"][0]
    # The EXACT count — never min(actual, backstop) dressed up as intake.
    assert inserted["new_jobs"] == 10
    assert inserted["window_truncated"] is True
    # A partial sample gets no verdict.
    assert inserted["tripwire_fired"] is False
    assert inserted["tripwire_distance"] is None
    assert "window truncated" in inserted["tripwire_reason"]


async def test_record_cycle_health_throttles_recent_rows(small_samples: None) -> None:
    recent = datetime.now(UTC) - timedelta(minutes=5)
    sb = _FakeSupabase(
        {"catalog_health_cycles": [[{"computed_at": recent.isoformat()}]]},
    )
    assert await record_cycle_health(sb) is None  # type: ignore[arg-type]
    assert sb.inserted == {}


async def test_record_cycle_health_swallows_failures() -> None:
    class _Exploding:
        def table(self, name: str) -> Any:
            raise RuntimeError("db down")

    # Telemetry only: a dead DB costs one row, never an exception into the
    # poll cycle.
    assert await record_cycle_health(_Exploding()) is None  # type: ignore[arg-type]


async def test_record_cycle_health_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "catalog_health_enabled", False)

    class _MustNotBeTouched:
        def table(self, name: str) -> Any:  # pragma: no cover - guard
            raise AssertionError("disabled recorder must not touch the DB")

    assert await record_cycle_health(_MustNotBeTouched()) is None  # type: ignore[arg-type]


# ---- admin endpoint ---------------------------------------------------------


def teardown_function() -> None:
    app.dependency_overrides.clear()


def test_admin_catalog_health_requires_operator_key() -> None:
    assert TestClient(app).get("/admin/catalog-health").status_code == 401


def test_admin_catalog_health_returns_recent_rows() -> None:
    rows = [
        {
            "computed_at": "2026-09-03T10:00:00+00:00",
            "window_started_at": "2026-09-02T10:00:00+00:00",
            "new_jobs": 120,
            "relevant_jobs": 80,
            "window_truncated": False,
            "live_total": 9000,
            "pct_ungraded": 41.2,
            "pct_location_unknown": 8.0,
            "family_counts": {"frontend": 900},
            "median_admission_age_hours": 6.5,
            "top_title_tokens": [["engineer", 60]],
            "tripwire_fired": False,
            "tripwire_distance": 0.12,
            "tripwire_reason": None,
        }
    ]
    sb = _FakeSupabase({"catalog_health_cycles": [rows]})
    app.dependency_overrides[get_async_service_supabase] = lambda: sb
    app.dependency_overrides[verify_api_key] = lambda: "test-api-key"

    resp = TestClient(app).get("/admin/catalog-health?limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["rows"][0]["new_jobs"] == 120
    assert body["rows"][0]["top_title_tokens"] == [["engineer", 60]]
    # The truncation flag must reach the operator: relevant_jobs/median/tokens
    # cover only the collected subset when it is true, and nothing else in
    # the response says so (#974 review).
    assert body["rows"][0]["window_truncated"] is False
