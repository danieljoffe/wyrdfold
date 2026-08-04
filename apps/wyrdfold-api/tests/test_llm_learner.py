"""Tests for the LLM-driven feedback learner (Doc 2 v2).

Covers the pure ``_apply_patch_to_profile`` helper plus the
auto-apply / stage / empty-patch paths of ``run_llm_learner`` against
an in-memory Supabase + LLM fake. The LLM is mocked at the
``complete_json`` boundary so we don't make real API calls.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest

from app.models.learning import ProfilePatch, RescoreProjection
from app.models.llm import LLMResult, LLMUsage
from app.services.llm_learner import (
    StagedPatchConflictError,
    _apply_patch_to_profile,
    _core_skill_keywords,
    _strip_self_colliding_negatives,
    apply_staged_patch,
    reject_staged_patch,
    run_llm_learner,
)

# ---- Self-colliding-negative guard (#47) ----------------------------------


class TestStripSelfCollidingNegatives:
    def _patch(self, negatives: list[str]) -> ProfilePatch:
        return ProfilePatch(add_negative=negatives, confidence=0.9, rationale="r")

    def test_drops_negative_matching_a_search_keyword(self) -> None:
        # "success" learned from "Customer Success" feedback would hard-zero the
        # user's own "customer success manager" roles.
        cleaned, dropped = _strip_self_colliding_negatives(
            self._patch(["success", "intern"]),
            search_keywords=["customer success manager", "cx lead"],
            core_skills=[],
        )
        assert dropped == ["success"]
        assert cleaned.add_negative == ["intern"]  # the legit one survives

    def test_drops_negative_matching_a_core_skill(self) -> None:
        cleaned, dropped = _strip_self_colliding_negatives(
            self._patch(["React"]),
            search_keywords=[],
            core_skills=["React", "TypeScript"],
        )
        assert dropped == ["React"]
        assert cleaned.add_negative == []

    def test_keeps_legit_negative_unrelated_to_own_terms(self) -> None:
        patch = self._patch(["sales"])
        cleaned, dropped = _strip_self_colliding_negatives(
            patch, search_keywords=["frontend engineer"], core_skills=["React"]
        )
        assert dropped == []
        assert cleaned is patch  # untouched, same object

    def test_multiword_negative_phrase_not_word_matching_is_kept(self) -> None:
        # `\bsales engineer\b` does not occur in "frontend engineer", so the
        # phrase negative is safe and kept (mirrors the real matcher).
        _, dropped = _strip_self_colliding_negatives(
            self._patch(["sales engineer"]),
            search_keywords=["frontend engineer"],
            core_skills=[],
        )
        assert dropped == []

    def test_collision_is_case_insensitive(self) -> None:
        _, dropped = _strip_self_colliding_negatives(
            self._patch(["SUCCESS"]),
            search_keywords=["Customer Success"],
            core_skills=[],
        )
        assert dropped == ["SUCCESS"]

    def test_noop_when_no_negatives_or_no_protected_terms(self) -> None:
        p1 = self._patch([])
        assert _strip_self_colliding_negatives(p1, search_keywords=["x"], core_skills=[]) == (
            p1,
            [],
        )
        p2 = self._patch(["junior"])
        assert _strip_self_colliding_negatives(p2, search_keywords=[], core_skills=[]) == (p2, [])


class TestCoreSkillKeywords:
    def test_extracts_core_skill_names(self) -> None:
        profile = {"categories": {"core_skills": {"keywords": {"React": 3, "Go": 2}}}}
        assert _core_skill_keywords(profile) == ["React", "Go"]

    def test_missing_core_skills_returns_empty(self) -> None:
        assert _core_skill_keywords({}) == []
        assert _core_skill_keywords({"categories": {}}) == []


# ---- Pure profile-patch arithmetic ----------------------------------------


class TestApplyPatchToProfile:
    def test_appends_new_negatives_dedup_case_insensitive(self) -> None:
        profile = {"negative": {"keywords": ["Junior"], "weight": -10.0}}
        patch = ProfilePatch(
            add_negative=["junior", "rep"],
            confidence=0.9,
            rationale="x",
        )
        out = _apply_patch_to_profile(profile, patch)
        # "junior" already present (case-insensitive), only "rep" added.
        assert out["negative"]["keywords"] == ["Junior", "rep"]

    def test_remove_negative_is_case_insensitive(self) -> None:
        profile = {"negative": {"keywords": ["Junior", "intern"], "weight": -10.0}}
        patch = ProfilePatch(
            remove_negative=["JUNIOR"],
            confidence=0.9,
            rationale="x",
        )
        out = _apply_patch_to_profile(profile, patch)
        assert out["negative"]["keywords"] == ["intern"]

    def test_add_secondary_creates_category_with_default_weight(self) -> None:
        profile: dict[str, Any] = {}
        patch = ProfilePatch(
            add_secondary={"Salesforce": 2, "Looker": 1},
            confidence=0.9,
            rationale="x",
        )
        out = _apply_patch_to_profile(profile, patch)
        secondary = out["categories"]["secondary_skills"]
        assert secondary["weight"] == 1.0
        assert secondary["keywords"] == {"Salesforce": 2, "Looker": 1}

    def test_add_secondary_clamps_weights_to_1_3_range(self) -> None:
        patch = ProfilePatch(
            add_secondary={"A": 9, "B": 0, "C": 2},
            confidence=0.9,
            rationale="x",
        )
        out = _apply_patch_to_profile({}, patch)
        kw = out["categories"]["secondary_skills"]["keywords"]
        # Clamp upper to 3 and lower to 1.
        assert kw["A"] == 3
        assert kw["B"] == 1
        assert kw["C"] == 2

    def test_demote_removes_keyword_from_any_category(self) -> None:
        profile = {
            "categories": {
                "core_skills": {"keywords": {"React": 3, "JQuery": 1}, "weight": 2.0},
                "secondary_skills": {"keywords": {"jquery": 1}, "weight": 1.0},
            }
        }
        patch = ProfilePatch(
            demote_keywords=["jQuery"],
            confidence=0.9,
            rationale="x",
        )
        out = _apply_patch_to_profile(profile, patch)
        # Both buckets lose any case-variant of "jquery".
        assert "JQuery" not in out["categories"]["core_skills"]["keywords"]
        assert out["categories"]["secondary_skills"]["keywords"] == {}
        assert "React" in out["categories"]["core_skills"]["keywords"]

    def test_input_profile_not_mutated(self) -> None:
        """``_apply_patch_to_profile`` returns a deep copy so callers
        can stash the original as ``prev_profile`` in the audit log."""
        profile: dict[str, Any] = {
            "negative": {"keywords": ["a"], "weight": -10.0},
        }
        patch = ProfilePatch(add_negative=["b"], confidence=0.9, rationale="x")
        _apply_patch_to_profile(profile, patch)
        assert profile["negative"]["keywords"] == ["a"]


# ---- run_llm_learner end-to-end (mock LLM + fake supabase) ----------------


class _Resp:
    """Awaitable fake response (#57 PR-G2e-3).

    Every DB read in ``run_llm_learner`` and its now-async projection
    ``project_profile_impact`` does ``await query.execute()``, so ``_Resp``
    is awaitable (yields itself) and also exposes ``.data`` / ``.count`` directly
    for the caller to read after the await. The apply-path tests leave the
    projection real: it awaits over empty ``scores`` (→ ``None``) exactly as
    before."""

    def __init__(self, data: Any) -> None:
        self.data = data
        self.count = len(data or [])

    def __await__(self) -> Any:
        async def _self() -> _Resp:
            return self

        return _self().__await__()


class _FakeQuery:
    def __init__(self, fake: _FakeSupabase, table: str) -> None:
        self._fake = fake
        self._table = table
        self._op: str | None = None
        self._payload: Any = None
        self._single = False

    def select(self, *_a: Any, **_k: Any) -> _FakeQuery:
        self._op = "select"
        return self

    def insert(self, payload: Any) -> _FakeQuery:
        self._op = "insert"
        self._payload = payload
        return self

    def update(self, payload: Any) -> _FakeQuery:
        self._op = "update"
        self._payload = payload
        return self

    def eq(self, _c: str, _v: Any) -> _FakeQuery:
        return self

    def is_(self, _c: str, _v: Any) -> _FakeQuery:
        return self

    def in_(self, _c: str, _v: Any) -> _FakeQuery:
        return self

    def order(self, *_a: Any, **_k: Any) -> _FakeQuery:
        return self

    def limit(self, _n: int) -> _FakeQuery:
        return self

    def single(self) -> _FakeQuery:
        self._single = True
        return self

    def execute(self) -> _Resp:
        self._fake.log.append({"table": self._table, "op": self._op, "payload": self._payload})
        data = self._fake.next_response(self._table, self._op)
        if self._single:
            data = data[0] if data else None
        return _Resp(data)


class _FakeSupabase:
    def __init__(self) -> None:
        self.log: list[dict[str, Any]] = []
        self._responses: list[tuple[str, str | None, Any]] = []

    def push(self, table: str, op: str | None, data: Any) -> None:
        self._responses.append((table, op, data))

    def next_response(self, table: str, op: str | None) -> Any:
        for i, (t, o, _) in enumerate(self._responses):
            if t == table and o == op:
                return self._responses.pop(i)[2]
        return []

    def table(self, name: str) -> _FakeQuery:
        return _FakeQuery(self, name)

    def rpc(self, fn: str, params: dict[str, Any]) -> _FakeQuery:
        q = _FakeQuery(self, fn)
        q._op = "rpc"
        q._payload = params
        return q


def _fb_row(reason: str = "sales role") -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    return {
        "id": "fb-" + reason[:6],
        "user_id": "u",
        "job_posting_id": "j-" + reason[:6],
        "target_id": "t",
        "signal": "irrelevant",
        "reason": reason,
        "applied_at": None,
        "applied_run_id": None,
        "created_at": now,
        "updated_at": now,
    }


def _target_row(profile_version: int = 1) -> dict[str, Any]:
    return {
        "id": "t",
        "scoring_profile": {
            "negative": {"keywords": ["junior"], "weight": -10.0},
        },
        "profile_version": profile_version,
    }


def _llm_result() -> LLMResult:
    return LLMResult(
        content="{}",
        model="claude-sonnet-4-6",
        usage=LLMUsage(input_tokens=100, output_tokens=50),
        cost_usd=0.001,
        latency_ms=500,
    )


@pytest.fixture()
def fake() -> _FakeSupabase:
    return _FakeSupabase()


@pytest.mark.asyncio
async def test_below_threshold_returns_none(fake: _FakeSupabase) -> None:
    fake.push("job_feedback", "select", [_fb_row() for _ in range(2)])
    with patch("app.services.llm_learner.complete_json") as mock_complete:
        result = await run_llm_learner(
            fake,
            object(),
            user_id="u",
            target_id="t",
        )
    assert result is None
    mock_complete.assert_not_called()


@pytest.mark.asyncio
async def test_high_confidence_patch_auto_applies(
    fake: _FakeSupabase,
) -> None:
    fake.push("job_feedback", "select", [_fb_row() for _ in range(3)])
    fake.push("targets", "select", [_target_row(profile_version=1)])
    fake.push("jobs", "select", [{"id": "j-sales ", "title": "Sales Rep"}])
    # The mutate path goes through the #191 RPC, then logs + stamps:
    fake.push(
        "apply_target_profile_patch",
        "rpc",
        [{"outcome": "applied", "new_version": 2}],
    )
    fake.push(
        "target_learning_log",
        "insert",
        [
            {
                "id": "run-1",
                "user_id": "u",
                "target_id": "t",
                "status": "applied",
                "prev_profile": {},
                "next_profile": {},
                "diff": {},
                "confidence": 0.9,
                "rationale": "r",
                "signals_consumed": 3,
                "applied_run_id": "rid",
                "created_at": datetime.now(UTC).isoformat(),
                "updated_at": datetime.now(UTC).isoformat(),
            }
        ],
    )
    fake.push("job_feedback", "update", [{"id": "fb-sales"}])

    patch_obj = ProfilePatch(
        add_negative=["sales"],
        confidence=0.9,
        rationale="3 sales-rep titles marked irrelevant",
    )
    with patch(
        "app.services.llm_learner.complete_json",
        return_value=(patch_obj, _llm_result()),
    ):
        result = await run_llm_learner(
            fake,
            object(),
            user_id="u",
            target_id="t",
        )

    assert result is not None
    assert result.applied is True
    assert result.profile_version_after == 2

    # The write went through the RPC — never a raw targets update — carrying
    # the acting user and the version the patch was computed against.
    assert [r for r in fake.log if r["table"] == "targets" and r["op"] == "update"] == []
    rpc_calls = [
        r for r in fake.log if r["table"] == "apply_target_profile_patch" and r["op"] == "rpc"
    ]
    assert len(rpc_calls) == 1
    params = rpc_calls[0]["payload"]
    assert params["p_user_id"] == "u"
    assert params["p_target_id"] == "t"
    assert params["p_expected_version"] == 1
    assert "sales" in params["p_next_profile"]["negative"]["keywords"]


@pytest.mark.asyncio
async def test_auto_apply_version_conflict_stages_instead(
    fake: _FakeSupabase,
) -> None:
    """#191: if the shared profile moved while the learn ran (concurrent
    merge / learn), the RPC refuses the stale patch — it must be STAGED for
    review, never clobber the newer profile, and feedback stays unstamped."""
    fake.push("job_feedback", "select", [_fb_row() for _ in range(3)])
    fake.push("targets", "select", [_target_row(profile_version=1)])
    fake.push("jobs", "select", [{"id": "j-sales ", "title": "Sales Rep"}])
    fake.push(
        "apply_target_profile_patch",
        "rpc",
        [{"outcome": "version_conflict", "new_version": 2}],
    )
    fake.push(
        "target_learning_log",
        "insert",
        [
            {
                "id": "race-1",
                "user_id": "u",
                "target_id": "t",
                "status": "staged",
                "prev_profile": {},
                "next_profile": {},
                "diff": {},
                "confidence": 0.9,
                "rationale": "r",
                "signals_consumed": 3,
                "applied_run_id": None,
                "created_at": datetime.now(UTC).isoformat(),
                "updated_at": datetime.now(UTC).isoformat(),
            }
        ],
    )

    patch_obj = ProfilePatch(add_negative=["sales"], confidence=0.9, rationale="confident")
    with patch(
        "app.services.llm_learner.complete_json",
        return_value=(patch_obj, _llm_result()),
    ):
        result = await run_llm_learner(
            fake,
            object(),
            user_id="u",
            target_id="t",
        )

    assert result is not None
    assert result.applied is False
    # Staged with the race note; no raw target write, no feedback stamp.
    log_insert = next(
        r for r in fake.log if r["table"] == "target_learning_log" and r["op"] == "insert"
    )
    assert log_insert["payload"]["status"] == "staged"
    assert "changed during the learn run" in log_insert["payload"]["rationale"]
    assert [r for r in fake.log if r["table"] == "targets" and r["op"] == "update"] == []
    assert [r for r in fake.log if r["table"] == "job_feedback" and r["op"] == "update"] == []


@pytest.mark.asyncio
async def test_auto_apply_refused_for_non_follower_writes_nothing(
    fake: _FakeSupabase,
) -> None:
    """#191: the in-DB follower re-check refusing the write (link severed
    mid-run, or a caller that skipped the router's ownership check) must
    leave no trace — no log row, no feedback stamp."""
    fake.push("job_feedback", "select", [_fb_row() for _ in range(3)])
    fake.push("targets", "select", [_target_row(profile_version=1)])
    fake.push("jobs", "select", [{"id": "j-sales ", "title": "Sales Rep"}])
    fake.push(
        "apply_target_profile_patch",
        "rpc",
        [{"outcome": "not_a_follower", "new_version": 1}],
    )

    patch_obj = ProfilePatch(add_negative=["sales"], confidence=0.9, rationale="confident")
    with patch(
        "app.services.llm_learner.complete_json",
        return_value=(patch_obj, _llm_result()),
    ):
        result = await run_llm_learner(
            fake,
            object(),
            user_id="u",
            target_id="t",
        )

    assert result is None
    assert [r for r in fake.log if r["table"] == "targets" and r["op"] == "update"] == []
    assert [r for r in fake.log if r["table"] == "target_learning_log"] == []
    assert [r for r in fake.log if r["table"] == "job_feedback" and r["op"] == "update"] == []


@pytest.mark.asyncio
async def test_high_confidence_outlier_patch_is_staged_by_learning_rate_cap(
    fake: _FakeSupabase,
) -> None:
    """A confident patch that the re-score projection flags as an outlier is
    staged for review, NOT auto-applied — the learning-rate cap (#5 P4)."""
    fake.push("job_feedback", "select", [_fb_row() for _ in range(3)])
    fake.push("targets", "select", [_target_row(profile_version=1)])
    fake.push("jobs", "select", [{"id": "j-sales ", "title": "Sales Rep"}])
    fake.push(
        "target_learning_log",
        "insert",
        [
            {
                "id": "cap-1",
                "user_id": "u",
                "target_id": "t",
                "status": "staged",
                "prev_profile": {},
                "next_profile": {},
                "diff": {},
                "confidence": 0.95,
                "rationale": "outlier",
                "signals_consumed": 3,
                "applied_run_id": None,
                "created_at": datetime.now(UTC).isoformat(),
                "updated_at": datetime.now(UTC).isoformat(),
            }
        ],
    )

    capped = RescoreProjection(
        jobs_considered=20,
        jobs_moved=12,
        moved_fraction=0.6,
        max_abs_delta=40,
        move_threshold=20,
        max_moved_fraction=0.30,
        capped=True,
    )
    patch_obj = ProfilePatch(
        add_negative=["sales"], confidence=0.95, rationale="confident but broad"
    )
    with (
        patch(
            "app.services.llm_learner.complete_json",
            return_value=(patch_obj, _llm_result()),
        ),
        patch(
            "app.services.llm_learner._project_patch_impact",
            return_value=capped,
        ),
    ):
        result = await run_llm_learner(
            fake,
            object(),
            user_id="u",
            target_id="t",
        )

    assert result is not None
    assert result.applied is False
    # High confidence, yet NOT applied: no target mutation, no feedback stamp.
    assert [r for r in fake.log if r["table"] == "targets" and r["op"] == "update"] == []
    assert [r for r in fake.log if r["table"] == "job_feedback" and r["op"] == "update"] == []
    # The staged row records the projection + an auto-stage note in the rationale.
    log_insert = next(
        r for r in fake.log if r["table"] == "target_learning_log" and r["op"] == "insert"
    )
    assert log_insert["payload"]["status"] == "staged"
    assert log_insert["payload"]["projection"] == capped.model_dump(mode="json")
    assert "learning-rate cap" in log_insert["payload"]["rationale"]


@pytest.mark.asyncio
async def test_low_confidence_patch_stages_without_mutating_target(
    fake: _FakeSupabase,
) -> None:
    fake.push("job_feedback", "select", [_fb_row() for _ in range(3)])
    fake.push("targets", "select", [_target_row()])
    fake.push("jobs", "select", [{"id": "j-sales ", "title": "Sales Rep"}])
    fake.push(
        "target_learning_log",
        "insert",
        [
            {
                "id": "stage-1",
                "user_id": "u",
                "target_id": "t",
                "status": "staged",
                "prev_profile": {},
                "next_profile": {},
                "diff": {},
                "confidence": 0.4,
                "rationale": "uncertain",
                "signals_consumed": 3,
                "applied_run_id": None,
                "created_at": datetime.now(UTC).isoformat(),
                "updated_at": datetime.now(UTC).isoformat(),
            }
        ],
    )

    patch_obj = ProfilePatch(
        add_negative=["sales"],
        confidence=0.4,
        rationale="uncertain",
    )
    with patch(
        "app.services.llm_learner.complete_json",
        return_value=(patch_obj, _llm_result()),
    ):
        result = await run_llm_learner(
            fake,
            object(),
            user_id="u",
            target_id="t",
        )

    assert result is not None
    assert result.applied is False
    # Crucially: NO targets update, NO job_feedback stamp.
    target_updates = [r for r in fake.log if r["table"] == "targets" and r["op"] == "update"]
    assert target_updates == []
    feedback_updates = [r for r in fake.log if r["table"] == "job_feedback" and r["op"] == "update"]
    assert feedback_updates == []


@pytest.mark.asyncio
async def test_empty_patch_consumes_feedback_without_mutating_profile(
    fake: _FakeSupabase,
) -> None:
    """High-confidence empty patch = "nothing learnable, this batch was
    noise". Stamp the rows consumed so we don't keep re-asking the LLM."""
    fake.push("job_feedback", "select", [_fb_row() for _ in range(3)])
    fake.push("targets", "select", [_target_row()])
    fake.push("jobs", "select", [])
    fake.push(
        "target_learning_log",
        "insert",
        [
            {
                "id": "noop-1",
                "user_id": "u",
                "target_id": "t",
                "status": "applied",
                "prev_profile": {},
                "next_profile": {},
                "diff": {},
                "confidence": 0.9,
                "rationale": "noise",
                "signals_consumed": 3,
                "applied_run_id": "rid",
                "created_at": datetime.now(UTC).isoformat(),
                "updated_at": datetime.now(UTC).isoformat(),
            }
        ],
    )
    fake.push("job_feedback", "update", [{"id": "fb-sales"}])

    patch_obj = ProfilePatch(confidence=0.9, rationale="no learnable pattern")
    with patch(
        "app.services.llm_learner.complete_json",
        return_value=(patch_obj, _llm_result()),
    ):
        result = await run_llm_learner(
            fake,
            object(),
            user_id="u",
            target_id="t",
        )

    assert result is not None
    assert result.applied is True
    # No target mutation despite the apply — empty patch is a no-op write.
    target_updates = [r for r in fake.log if r["table"] == "targets" and r["op"] == "update"]
    assert target_updates == []
    # But feedback WAS stamped so we don't loop on the same batch.
    feedback_updates = [r for r in fake.log if r["table"] == "job_feedback" and r["op"] == "update"]
    assert feedback_updates, "expected feedback rows stamped consumed"


# ---- apply_staged_patch / reject_staged_patch -----------------------------


async def test_reject_staged_patch_does_not_stamp_feedback(
    fake: _FakeSupabase,
) -> None:
    """Rejecting a stage means "wrong interpretation, try again later"
    — the underlying feedback rows must stay unapplied so a future learn
    run can revisit them with the same evidence."""
    fake.push(
        "target_learning_log",
        "update",
        [
            {
                "id": "stage-1",
                "user_id": "u",
                "target_id": "t",
                "status": "rejected",
                "prev_profile": {},
                "next_profile": {},
                "diff": {},
                "confidence": 0.4,
                "rationale": "x",
                "signals_consumed": 3,
                "applied_run_id": None,
                "created_at": datetime.now(UTC).isoformat(),
                "updated_at": datetime.now(UTC).isoformat(),
            }
        ],
    )
    result = await reject_staged_patch(fake, user_id="u", run_id="stage-1")  # type: ignore[arg-type]
    assert result is not None
    assert result.applied is False
    # No job_feedback writes.
    feedback_updates = [r for r in fake.log if r["table"] == "job_feedback"]
    assert feedback_updates == []


async def test_apply_staged_patch_returns_none_when_no_match(
    fake: _FakeSupabase,
) -> None:
    """Apply against an unknown / wrong-user run_id is a 404 path."""
    fake.push("target_learning_log", "select", [])  # single() → None
    result = await apply_staged_patch(fake, user_id="u", run_id="missing")  # type: ignore[arg-type]
    assert result is None


def _staged_log_row(status: str = "staged") -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    return {
        "id": "stage-1",
        "user_id": "u",
        "target_id": "t",
        "status": status,
        # Stage-time snapshot — deliberately computed against an OLD profile
        # so the recompute tests can prove the snapshot is NOT what gets
        # written (Copilot on #202).
        "prev_profile": {},
        "next_profile": {"negative": {"keywords": ["sales"]}},
        "diff": {
            "add_negative": ["sales"],
            "remove_negative": [],
            "add_secondary": {},
            "demote_keywords": [],
            "confidence": 0.4,
            "rationale": "r",
        },
        "confidence": 0.4,
        "rationale": "r",
        "signals_consumed": 3,
        "applied_run_id": None,
        "created_at": now,
        "updated_at": now,
    }


async def test_apply_staged_patch_goes_through_rpc(fake: _FakeSupabase) -> None:
    """#191: the human-approved apply also writes via the RPC (in-DB
    follower re-check + version guard), never a raw targets update — and it
    re-applies the PATCH to the current profile, not the stage-time
    snapshot."""
    fake.push("target_learning_log", "select", [_staged_log_row()])
    fake.push("targets", "select", [_target_row(profile_version=3)])
    fake.push(
        "apply_target_profile_patch",
        "rpc",
        [{"outcome": "applied", "new_version": 4}],
    )
    applied_row = _staged_log_row(status="applied")
    applied_row["applied_run_id"] = "rid-2"
    fake.push("target_learning_log", "update", [applied_row])
    fake.push("job_feedback", "select", [{"id": "fb-1"}])
    fake.push("job_feedback", "update", [{"id": "fb-1"}])

    result = await apply_staged_patch(fake, user_id="u", run_id="stage-1")  # type: ignore[arg-type]

    assert result is not None
    assert result.applied is True
    assert result.profile_version_after == 4
    assert [r for r in fake.log if r["table"] == "targets" and r["op"] == "update"] == []
    rpc_calls = [
        r for r in fake.log if r["table"] == "apply_target_profile_patch" and r["op"] == "rpc"
    ]
    assert len(rpc_calls) == 1
    assert rpc_calls[0]["payload"]["p_expected_version"] == 3
    # Recomputed from the CURRENT profile (["junior"]) + the patch ("sales")
    # — NOT the stage-time snapshot (["sales"] alone).
    assert rpc_calls[0]["payload"]["p_next_profile"]["negative"]["keywords"] == [
        "junior",
        "sales",
    ]


async def test_apply_staged_patch_preserves_intervening_changes(
    fake: _FakeSupabase,
) -> None:
    """Copilot on #202: a reference-JD merge (or another learn) landing
    between staging and apply must SURVIVE the apply — the patch's edits are
    re-applied to the current profile; the stale snapshot never overwrites
    the newer state. The log row's prev/next are updated to the transition
    that actually happened, keeping one-row revert truthful."""
    fake.push("target_learning_log", "select", [_staged_log_row()])
    current_profile = {
        # Added by a merge AFTER the patch was staged — must survive.
        "categories": {"core_skills": {"keywords": {"django": 3}, "weight": 2.0}},
        "negative": {"keywords": ["junior"], "weight": -10.0},
    }
    fake.push(
        "targets",
        "select",
        [{"id": "t", "scoring_profile": current_profile, "profile_version": 5}],
    )
    fake.push(
        "apply_target_profile_patch",
        "rpc",
        [{"outcome": "applied", "new_version": 6}],
    )
    applied_row = _staged_log_row(status="applied")
    applied_row["applied_run_id"] = "rid-2"
    fake.push("target_learning_log", "update", [applied_row])
    fake.push("job_feedback", "select", [])

    result = await apply_staged_patch(fake, user_id="u", run_id="stage-1")  # type: ignore[arg-type]

    assert result is not None
    assert result.profile_version_after == 6
    params = next(
        r for r in fake.log if r["table"] == "apply_target_profile_patch" and r["op"] == "rpc"
    )["payload"]
    assert params["p_expected_version"] == 5
    # The intervening merge's category is preserved AND the patch applied.
    written = params["p_next_profile"]
    assert written["categories"]["core_skills"]["keywords"] == {"django": 3}
    assert written["negative"]["keywords"] == ["junior", "sales"]
    # The log flip records the ACTUAL transition, not the stale snapshot —
    # including `diff` (the UI renders it; Copilot on #203). No strip
    # happened here, so it equals the staged patch.
    log_update = next(
        r for r in fake.log if r["table"] == "target_learning_log" and r["op"] == "update"
    )["payload"]
    assert log_update["prev_profile"] == current_profile
    assert log_update["next_profile"] == written
    assert log_update["diff"]["add_negative"] == ["sales"]


async def test_apply_staged_patch_strip_at_apply_is_reflected_in_log_diff(
    fake: _FakeSupabase,
) -> None:
    """Copilot on #203: if the apply-time #47 guard drops a now-self-
    colliding negative, the applied log row's `diff` (and rationale) must
    show what was ACTUALLY applied — not the staged edits the UI would
    otherwise misreport."""
    fake.push("target_learning_log", "select", [_staged_log_row()])
    fake.push(
        "targets",
        "select",
        [
            {
                "id": "t",
                "scoring_profile": {
                    "negative": {"keywords": ["junior"], "weight": -10.0},
                },
                "profile_version": 5,
                # Changed since staging: "sales" now collides with the
                # target's own search terms → the patch's negative must drop.
                "search_keywords": ["sales engineer"],
            }
        ],
    )
    fake.push(
        "apply_target_profile_patch",
        "rpc",
        [{"outcome": "applied", "new_version": 6}],
    )
    applied_row = _staged_log_row(status="applied")
    applied_row["applied_run_id"] = "rid-2"
    fake.push("target_learning_log", "update", [applied_row])
    fake.push("job_feedback", "select", [])

    result = await apply_staged_patch(fake, user_id="u", run_id="stage-1")  # type: ignore[arg-type]

    assert result is not None
    # The colliding negative was NOT applied…
    params = next(
        r for r in fake.log if r["table"] == "apply_target_profile_patch" and r["op"] == "rpc"
    )["payload"]
    assert params["p_next_profile"]["negative"]["keywords"] == ["junior"]
    # …and the audit row says so: diff shrank, rationale carries the note.
    log_update = next(
        r for r in fake.log if r["table"] == "target_learning_log" and r["op"] == "update"
    )["payload"]
    assert log_update["diff"]["add_negative"] == []
    assert "self-colliding negatives" in log_update["rationale"]


async def test_apply_staged_patch_conflict_raises_and_stays_staged(
    fake: _FakeSupabase,
) -> None:
    """#191: a concurrent write between the version read and the RPC apply
    surfaces as StagedPatchConflictError (the router's 409) — and the log
    row must NOT be flipped to applied."""
    fake.push("target_learning_log", "select", [_staged_log_row()])
    fake.push("targets", "select", [_target_row(profile_version=3)])
    fake.push(
        "apply_target_profile_patch",
        "rpc",
        [{"outcome": "version_conflict", "new_version": 4}],
    )

    with pytest.raises(StagedPatchConflictError):
        await apply_staged_patch(fake, user_id="u", run_id="stage-1")  # type: ignore[arg-type]

    # No status flip, no feedback stamp — the stage is still reviewable.
    assert [
        r for r in fake.log if r["table"] == "target_learning_log" and r["op"] == "update"
    ] == []
    assert [r for r in fake.log if r["table"] == "job_feedback"] == []
