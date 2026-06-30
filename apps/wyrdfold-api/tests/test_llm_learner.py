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
        assert _strip_self_colliding_negatives(p1, search_keywords=["x"], core_skills=[]) == (p1, [])
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
        patch = ProfilePatch(
            add_negative=["b"], confidence=0.9, rationale="x"
        )
        _apply_patch_to_profile(profile, patch)
        assert profile["negative"]["keywords"] == ["a"]


# ---- run_llm_learner end-to-end (mock LLM + fake supabase) ----------------


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

    def execute(self) -> Any:
        self._fake.log.append(
            {"table": self._table, "op": self._op, "payload": self._payload}
        )
        data = self._fake.next_response(self._table, self._op)
        if self._single:
            data = data[0] if data else None
        return type("Resp", (), {"data": data, "count": len(data or [])})()


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
    with patch(
        "app.services.llm_learner.complete_json"
    ) as mock_complete:
        result = await run_llm_learner(
            fake, object(), user_id="u", target_id="t"  # type: ignore[arg-type]
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
    # The mutate path will issue these writes:
    fake.push("targets", "update", [{"id": "t"}])
    fake.push("target_learning_log", "insert", [
        {
            "id": "run-1", "user_id": "u", "target_id": "t",
            "status": "applied",
            "prev_profile": {}, "next_profile": {}, "diff": {},
            "confidence": 0.9, "rationale": "r", "signals_consumed": 3,
            "applied_run_id": "rid", "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
        }
    ])
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
            fake, object(), user_id="u", target_id="t"  # type: ignore[arg-type]
        )

    assert result is not None
    assert result.applied is True
    assert result.profile_version_after == 2

    # The target update wrote profile_version=2.
    target_updates = [r for r in fake.log if r["table"] == "targets" and r["op"] == "update"]
    assert target_updates, "expected a targets update"
    payload = target_updates[0]["payload"]
    assert payload["profile_version"] == 2


@pytest.mark.asyncio
async def test_high_confidence_outlier_patch_is_staged_by_learning_rate_cap(
    fake: _FakeSupabase,
) -> None:
    """A confident patch that the re-score projection flags as an outlier is
    staged for review, NOT auto-applied — the learning-rate cap (#5 P4)."""
    fake.push("job_feedback", "select", [_fb_row() for _ in range(3)])
    fake.push("targets", "select", [_target_row(profile_version=1)])
    fake.push("jobs", "select", [{"id": "j-sales ", "title": "Sales Rep"}])
    fake.push("target_learning_log", "insert", [
        {
            "id": "cap-1", "user_id": "u", "target_id": "t",
            "status": "staged",
            "prev_profile": {}, "next_profile": {}, "diff": {},
            "confidence": 0.95, "rationale": "outlier",
            "signals_consumed": 3, "applied_run_id": None,
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
        }
    ])

    capped = RescoreProjection(
        jobs_considered=20, jobs_moved=12, moved_fraction=0.6,
        max_abs_delta=40, move_threshold=20, max_moved_fraction=0.30, capped=True,
    )
    patch_obj = ProfilePatch(
        add_negative=["sales"], confidence=0.95, rationale="confident but broad"
    )
    with patch(
        "app.services.llm_learner.complete_json",
        return_value=(patch_obj, _llm_result()),
    ), patch(
        "app.services.llm_learner._project_patch_impact",
        return_value=capped,
    ):
        result = await run_llm_learner(
            fake, object(), user_id="u", target_id="t"  # type: ignore[arg-type]
        )

    assert result is not None
    assert result.applied is False
    # High confidence, yet NOT applied: no target mutation, no feedback stamp.
    assert [r for r in fake.log if r["table"] == "targets" and r["op"] == "update"] == []
    assert [r for r in fake.log if r["table"] == "job_feedback" and r["op"] == "update"] == []
    # The staged row records the projection + an auto-stage note in the rationale.
    log_insert = next(
        r for r in fake.log
        if r["table"] == "target_learning_log" and r["op"] == "insert"
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
    fake.push("target_learning_log", "insert", [
        {
            "id": "stage-1", "user_id": "u", "target_id": "t",
            "status": "staged",
            "prev_profile": {}, "next_profile": {}, "diff": {},
            "confidence": 0.4, "rationale": "uncertain",
            "signals_consumed": 3, "applied_run_id": None,
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
        }
    ])

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
            fake, object(), user_id="u", target_id="t"  # type: ignore[arg-type]
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
    fake.push("target_learning_log", "insert", [
        {
            "id": "noop-1", "user_id": "u", "target_id": "t",
            "status": "applied",
            "prev_profile": {}, "next_profile": {}, "diff": {},
            "confidence": 0.9, "rationale": "noise",
            "signals_consumed": 3, "applied_run_id": "rid",
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
        }
    ])
    fake.push("job_feedback", "update", [{"id": "fb-sales"}])

    patch_obj = ProfilePatch(
        confidence=0.9, rationale="no learnable pattern"
    )
    with patch(
        "app.services.llm_learner.complete_json",
        return_value=(patch_obj, _llm_result()),
    ):
        result = await run_llm_learner(
            fake, object(), user_id="u", target_id="t"  # type: ignore[arg-type]
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


def test_reject_staged_patch_does_not_stamp_feedback(
    fake: _FakeSupabase,
) -> None:
    """Rejecting a stage means "wrong interpretation, try again later"
    — the underlying feedback rows must stay unapplied so a future learn
    run can revisit them with the same evidence."""
    fake.push("target_learning_log", "update", [
        {
            "id": "stage-1", "user_id": "u", "target_id": "t",
            "status": "rejected",
            "prev_profile": {}, "next_profile": {}, "diff": {},
            "confidence": 0.4, "rationale": "x",
            "signals_consumed": 3, "applied_run_id": None,
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
        }
    ])
    result = reject_staged_patch(fake, user_id="u", run_id="stage-1")  # type: ignore[arg-type]
    assert result is not None
    assert result.applied is False
    # No job_feedback writes.
    feedback_updates = [r for r in fake.log if r["table"] == "job_feedback"]
    assert feedback_updates == []


def test_apply_staged_patch_returns_none_when_no_match(
    fake: _FakeSupabase,
) -> None:
    """Apply against an unknown / wrong-user run_id is a 404 path."""
    fake.push("target_learning_log", "select", [])  # single() → None
    result = apply_staged_patch(fake, user_id="u", run_id="missing")  # type: ignore[arg-type]
    assert result is None
