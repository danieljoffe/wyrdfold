"""One ``min_score`` floor rule, three implementations — pinned together.

THE BUG THIS EXISTS TO PREVENT (2026-08-08, twice in one week):
The floor's "which rows are exempt" rule is implemented in three places, and
each release fixed a different subset of them.

  * ``_apply_score_floor``    — app/routers/jobs.py, the two-query path
  * ``pipeline_counts``       — SQL RPC, the dashboard status tiles
  * ``get_cross_target_jobs`` — SQL RPC, /jobs with no target filter

All three keyed the exemption on ``scoring_status != 'complete'``. That column
is not the graded signal — ``_is_pending``'s docstring says so outright, since
'complete' is set on rows that were never actually graded. The first fix
(release #663) repointed the Python helper and ``pipeline_counts`` and MISSED
``get_cross_target_jobs``, which is the path that actually serves the list. So
``min_score=70`` still returned non-pending rows scored 69/65/65/62/62 in prod,
and the release looked green because nothing tied the three sites to one rule.

There is a fourth call site, ``get_target_jobs``, which is deliberately never
floored — the router bails to the two-query path instead. That bail-out is
load-bearing (the RPC's floor is a flat ``score >= p_min_score`` that would hide
Pending rows), so it is pinned here too.

These tests are intentionally static-analysis-flavoured. A behavioural test
needs a live Postgres and only covers whichever RPC it calls; the failure mode
here is *a site nobody looked at*, so the assertions are over the whole corpus
of sites and fail when a new one appears.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.routers.jobs import _apply_score_floor, _is_pending

# tests/ -> wyrdfold-api -> apps -> repo root -> supabase/migrations
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "supabase" / "migrations"
JOBS_ROUTER = Path(__file__).resolve().parents[1] / "app" / "routers" / "jobs.py"

# The predicate forms that caused the defect. ``scoring_status`` may still be
# SELECTed and returned (it is a response column) — only its use as the floor's
# exemption test is banned.
_BANNED_EXEMPTION_FORMS = (
    "scoring_status IS DISTINCT FROM 'complete'",
    "scoring_status != 'complete'",
    "scoring_status <> 'complete'",
    "scoring_status = 'complete'",
)

# The two accepted spellings of the graded signal. ``is_graded`` is a
# trigger-maintained denormalisation of ``axis_scores IS NOT NULL`` — see
# ``test_is_graded_denorm_is_exactly_the_axis_scores_signal``, which is what
# licenses the second spelling.
_GRADED_SIGNAL_FORMS = (
    "axis_scores IS NULL",
    "NOT s.is_graded",
)

# Every RPC that applies the floor inside its own SQL body. Adding one without
# adding it here trips ``test_no_unpinned_floor_call_sites``.
_FLOORED_RPCS = ("pipeline_counts", "get_cross_target_jobs")


_ANY_FUNCTION_HEADER = re.compile(
    r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+public\.", re.IGNORECASE
)


def _strip_sql_comments(sql: str) -> str:
    """Drop ``--`` line comments, tracking quote parity so a literal is safe.

    Necessary, not cosmetic: these migrations carry long explanatory headers
    that *quote the very predicate they removed*. Scanning raw text made this
    module's own regression assertion fire on the migration that fixed the bug.
    """
    out = []
    for line in sql.splitlines():
        in_quote = False
        for i, ch in enumerate(line):
            if ch == "'":
                in_quote = not in_quote
            elif ch == "-" and not in_quote and line[i : i + 2] == "--":
                line = line[:i]
                break
        out.append(line)
    return "\n".join(out)


def _latest_definition(fn_name: str) -> tuple[Path, str]:
    """The newest migration that (re)defines ``fn_name``, and that function's
    SQL body with comments stripped.

    Migrations are timestamp-prefixed and applied in filename order, so the
    lexicographically-last file containing a ``CREATE OR REPLACE FUNCTION`` for
    this name is the definition prod ends up running. Reading anything older
    would let a stale-but-passing definition mask a live-but-broken one.

    The returned text is scoped to this function alone — from its header to the
    next function header in the file — because several migrations define more
    than one function and a sibling's predicate must not satisfy (or trip) an
    assertion about this one.
    """
    pattern = re.compile(
        rf"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+public\.{re.escape(fn_name)}\s*\(",
        re.IGNORECASE,
    )
    hits = sorted(
        path for path in MIGRATIONS_DIR.glob("*.sql") if pattern.search(path.read_text())
    )
    if not hits:
        raise AssertionError(f"no migration defines public.{fn_name}")
    latest = hits[-1]

    text = latest.read_text()
    start = pattern.search(text)
    assert start is not None
    next_header = _ANY_FUNCTION_HEADER.search(text, start.end())
    body = text[start.start() : next_header.start() if next_header else len(text)]
    return latest, _strip_sql_comments(body)


@pytest.mark.parametrize("fn_name", _FLOORED_RPCS)
def test_floored_rpc_exempts_on_the_graded_signal(fn_name: str) -> None:
    """Each floored RPC keys its exemption on the graded signal."""
    path, sql = _latest_definition(fn_name)
    assert any(form in sql for form in _GRADED_SIGNAL_FORMS), (
        f"{path.name} defines public.{fn_name} but its floor does not exempt on "
        f"the graded signal (expected one of {_GRADED_SIGNAL_FORMS}). A floor "
        f"that cannot tell graded from Pending either hides ungraded jobs or "
        f"admits low-scored ones under a 'Score N+' chip."
    )


@pytest.mark.parametrize("fn_name", _FLOORED_RPCS)
def test_floored_rpc_does_not_exempt_on_scoring_status(fn_name: str) -> None:
    """THE regression. ``scoring_status`` must never gate the floor again."""
    path, sql = _latest_definition(fn_name)
    offenders = [form for form in _BANNED_EXEMPTION_FORMS if form in sql]
    assert offenders == [], (
        f"{path.name} gates public.{fn_name}'s score floor on {offenders}. "
        f"'complete' is set on rows that were never graded — in prod this "
        f"exempted 5,130 rows carrying real axis_scores from the floor while "
        f"the UI rendered them with a numeric badge, so 'Score 70+' returned "
        f"rows scored 62. Use the graded signal instead."
    )


@pytest.mark.parametrize("fn_name", _FLOORED_RPCS)
def test_floored_rpc_judges_the_aged_score(fn_name: str) -> None:
    """#665: the floor compares ``recency_score``, not ``score``.

    A wyrdfold score means "good match AND still fresh", so the aged number is
    the one the user sees and the one the chip must judge. Flooring the raw fit
    while displaying the aged value is what made "Score 70+" render a card
    reading 56 in prod. This is a separate assertion from the graded-signal one
    above: the floor can exempt the right ROWS while still comparing the wrong
    COLUMN — which is exactly the state prod shipped in for a day.
    """
    path, sql = _latest_definition(fn_name)
    assert "recency_score >=" in sql, (
        f"{path.name} floors public.{fn_name} on something other than "
        f"recency_score. The list shows the aged score, so the floor must judge "
        f"the aged score or the chip lies about its own results."
    )
    assert "s.score >=" not in sql, (
        f"{path.name} still compares the RAW score in public.{fn_name}'s floor."
    )


def test_recency_score_cannot_be_null() -> None:
    """The floor's silent-disappearance guard.

    ``recency_score >= N`` evaluates NULL for a NULL row, so a row that missed
    the writer would be EXCLUDED from every floored list — gone, not degraded.
    The fix lives at the write site (trigger fill + NOT NULL) rather than as a
    COALESCE at each read site, because PostgREST — which the two-query floor
    filters through — cannot express COALESCE, so a read-side patch makes the
    RPC and the Python path disagree. Measured: read-side COALESCE took the
    integration suite from 13 failures to 36; the write-side fix took it to 0.
    """
    _, sql = _latest_definition("scores_sync_denorm")
    assert "NEW.recency_score := COALESCE(NEW.recency_score, NEW.score);" in sql, (
        "scores_sync_denorm no longer fills recency_score. Without it a NULL "
        "silently drops the row from every floored list."
    )
    ddl = "\n".join(
        p.read_text() for p in MIGRATIONS_DIR.glob("*.sql") if "recency_score" in p.read_text()
    )
    assert "ALTER COLUMN recency_score SET NOT NULL" in ddl, (
        "recency_score is no longer NOT NULL — the schema must enforce what the "
        "trigger guarantees, or a future writer can reintroduce the hole."
    )


def test_cross_target_dedup_tiebreak_matches_prefer_score_row() -> None:
    """The per-job representative is chosen by the same number in both paths.

    ``get_cross_target_jobs`` picks it with ``DISTINCT ON ... ORDER BY``; the
    two-query path picks it with ``_prefer_score_row``. Same rule, two
    implementations — so the same job can be attributed to different targets
    depending on which path served the request if they drift. That is the #664
    failure mode, one layer down.
    """
    _, sql = _latest_definition("get_cross_target_jobs")
    assert "ORDER BY s.job_posting_id, s.is_graded DESC, s.recency_score DESC" in sql, (
        "the RPC's DISTINCT ON tiebreak no longer orders by recency_score"
    )
    source = JOBS_ROUTER.read_text()
    assert "def _representative_value(" in source, (
        "_prefer_score_row no longer routes through _representative_value, so "
        "the Python tiebreak can silently diverge from the RPC's"
    )
    assert 'stored = row.get("recency_score")' in source, (
        "_representative_value no longer compares recency_score — it must "
        "mirror the RPC's DISTINCT ON tiebreak"
    )


def test_python_floor_exempts_on_the_graded_signal() -> None:
    """``_apply_score_floor`` — the two-query path's copy of the same rule."""

    class _RecordingQuery:
        def __init__(self) -> None:
            self.filters: list[str] = []

        def or_(self, expr: str) -> _RecordingQuery:
            self.filters.append(expr)
            return self

    q = _RecordingQuery()
    _apply_score_floor(q, 70)
    assert q.filters == ["axis_scores.is.null,recency_score.gte.70"]
    assert not any("scoring_status" in f for f in q.filters)


def test_all_three_sites_agree_with_is_pending_row_by_row() -> None:
    """The rule itself: a row is floor-exempt exactly when it is Pending.

    This is the invariant the three implementations are supposed to share, so
    it is asserted against ``_is_pending`` — the classifier that drives the
    badge the user actually sees. A row exempted from the floor but badged as
    graded is the whole defect.
    """
    # (row as the list paths fetch it, is_graded as the trigger would set it)
    rows = [
        ({"scoring_status": "complete", "axis_scores": {"title_fit": 80}}, True),
        ({"scoring_status": "stage2", "axis_scores": {"title_fit": 62}}, True),
        ({"scoring_status": "stage1", "axis_scores": {"title_fit": 55}}, True),
        # 'complete' with no grade — 28 such rows live in prod. Genuinely
        # Pending, so genuinely exempt.
        ({"scoring_status": "complete", "axis_scores": None}, False),
        ({"scoring_status": "stage1", "axis_scores": None}, False),
        ({"scoring_status": "stage2", "axis_scores": None}, False),
    ]

    for row, is_graded in rows:
        pending = _is_pending(row)

        # Site 1 — Python: ``axis_scores.is.null`` is the exemption leg.
        python_exempt = row["axis_scores"] is None
        # Site 2 — pipeline_counts SQL: ``s.axis_scores IS NULL``.
        pipeline_counts_exempt = row["axis_scores"] is None
        # Site 3 — get_cross_target_jobs SQL: ``NOT s.is_graded``.
        cross_target_exempt = not is_graded

        assert python_exempt is pending, f"_apply_score_floor disagrees with the badge on {row}"
        assert pipeline_counts_exempt is pending, f"pipeline_counts disagrees on {row}"
        assert cross_target_exempt is pending, f"get_cross_target_jobs disagrees on {row}"

        # And the point of the whole exercise — the three agree with each other.
        assert python_exempt == pipeline_counts_exempt == cross_target_exempt, (
            f"the three floor implementations disagree on {row}: "
            f"python={python_exempt} pipeline_counts={pipeline_counts_exempt} "
            f"cross_target={cross_target_exempt}"
        )


def test_is_graded_denorm_is_exactly_the_axis_scores_signal() -> None:
    """``get_cross_target_jobs`` spells the rule ``NOT s.is_graded``.

    That is only equivalent to the other two sites because a BEFORE INSERT OR
    UPDATE trigger derives ``is_graded`` from ``axis_scores`` on every write
    (prod: 0/162,008 rows disagree, verified 2026-08-08). If that derivation
    ever changes, the cross-target floor silently stops matching the badge —
    so pin it here rather than trusting a comment.
    """
    _, sql = _latest_definition("scores_sync_denorm")
    assert "NEW.is_graded := (NEW.axis_scores IS NOT NULL);" in sql, (
        "scores_sync_denorm no longer derives is_graded from axis_scores. "
        "get_cross_target_jobs's floor keys on is_graded on the strength of "
        "that derivation — repoint it to axis_scores, or restore the trigger."
    )


def test_unfloored_rpc_stays_unfloored() -> None:
    """``get_target_jobs`` is the fourth ``p_min_score`` site and must stay
    unreachable when a floor is set.

    Its SQL applies a flat ``score >= p_min_score`` with no exemption, so it
    would hide every not-yet-graded row. The router bails to the two-query path
    instead; that guard is the only thing keeping the flat floor harmless.
    """
    source = JOBS_ROUTER.read_text()
    guard = re.search(
        r"if min_score and min_score > 0:\s*\n\s*raise _RpcIneligibleError\(", source
    )
    assert guard is not None, (
        "_list_jobs_for_target_rpc no longer bails out when min_score is set. "
        "get_target_jobs floors with a flat score >= p_min_score and cannot "
        "exempt Pending rows — either restore the bail-out or give that RPC the "
        "graded-signal exemption the other two have."
    )


def test_no_unpinned_floor_call_sites() -> None:
    """A census, so a fourth RPC cannot be added without landing here.

    The defect was not a wrong predicate — it was a site nobody remembered to
    look at. If this count changes, add the new site to ``_FLOORED_RPCS`` (or
    to the unfloored guard above) and give it the same rule.
    """
    source = JOBS_ROUTER.read_text()
    sites = re.findall(r'"p_min_score"\s*:', source)
    assert len(sites) == 3, (
        f"expected 3 p_min_score call sites in jobs.py, found {len(sites)}. "
        f"Every site must either exempt Pending rows on the graded signal or "
        f"be unreachable when a floor is set."
    )
