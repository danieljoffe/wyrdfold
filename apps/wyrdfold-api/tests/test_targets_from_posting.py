"""``POST /targets/from-posting/{id}`` (#1071).

Two defects shared one route. The posting's employer-decorated title became
``targets.normalized_label`` (the UNIQUE catalog key) with no canonicalization,
so every from-posting create minted a raw-title catalog row; and a failed
profile derivation was logged past, leaving a target that scored nothing with
nothing telling the user why. The route now runs the same order as
``from_input.from_url`` (cap preflight, canonicalize, match, atomic
create-and-link, derive) and maps derivation failures onto the #1066 taxonomy:
an explicit retryable error state for provider/model failures, and a
propagating server fault for anything else.

Shared-catalog safety is the load-bearing part: a target that already existed
(a match, or a concurrent create that won the exact key) is attached to, never
derived over, so one request can never overwrite a profile other users follow.

The route function is called directly with its collaborators monkeypatched in
the router's namespace; every fake appends to one ordered event log so the
tests can assert ORDER and ABSENCE, not just presence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import HTTPException

from app.models.llm import LLMResult, LLMUsage
from app.models.targets import DerivedTarget, JobTarget, ScoringProfile, TargetUpdate
from app.routers import targets as router
from app.services.llm import cost_log
from app.services.llm.errors import LLMMalformedOutputError, LLMUpstreamUnavailableError
from app.services.targets import crud, from_input
from app.services.targets.activation import ActivationError
from app.services.targets.normalize_posting_title import NormalizedTitle

POSTING_TITLE = "Senior Product Builder (Product Manager), Enterprise Readiness & Admin Platform"
CANONICAL = "Product Manager"
JD_TEXT = "We are hiring a product manager to own the enterprise readiness platform. " * 3


def _llm_result() -> LLMResult:
    return LLMResult(
        content="{}",
        model="claude-sonnet-4-6",
        usage=LLMUsage(input_tokens=1, output_tokens=1),
        cost_usd=0.0001,
        latency_ms=10,
    )


def _target(
    *, id: str = "t-new", label: str = CANONICAL, activation_status: str = "idle"
) -> JobTarget:
    now = datetime.now(UTC)
    return JobTarget(
        id=id,
        label=label,
        description=None,
        normalized_label=label.lower().strip(),
        scoring_profile=ScoringProfile(),
        search_keywords=[],
        activation_status=activation_status,
        profile_version=1,
        app_active=False,
        created_at=now,
        updated_at=now,
    )


class Harness:
    """Fakes for every collaborator, plus one ordered event log."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []
        self.matched: JobTarget | None = None
        self.description_html = f"<p>{JD_TEXT}</p>"
        self.cap = 3
        self.active_count = 0
        self.was_created = True
        self.create_error: BaseException | None = None
        self.derive_error: BaseException | None = None
        self.link_error: BaseException | None = None
        self.updates: list[TargetUpdate] = []

    def named(self, name: str) -> list[Any]:
        return [payload for n, payload in self.events if n == name]

    def order(self) -> list[str]:
        return [n for n, _ in self.events]


@pytest.fixture
def h(monkeypatch: pytest.MonkeyPatch) -> Harness:
    h = Harness()

    async def get_posting(supabase: Any, posting_id: str) -> dict[str, Any]:
        return {
            "id": posting_id,
            "title": POSTING_TITLE,
            "description_html": h.description_html,
            "absolute_url": "https://boards.example.com/jobs/1",
        }

    async def cap(supabase: Any, user_id: str) -> int:
        h.events.append(("cap", user_id))
        return h.cap

    async def count_active(supabase: Any, user_id: str) -> int:
        return h.active_count

    async def normalize(llm: Any, *, title: str, jd_text: str) -> tuple[NormalizedTitle, LLMResult]:
        h.events.append(("normalize", title))
        return NormalizedTitle(label=CANONICAL), _llm_result()

    async def record(*args: Any, **kwargs: Any) -> None:
        h.events.append(("cost_log", kwargs.get("purpose")))

    async def match(supabase: Any, label: str) -> JobTarget | None:
        h.events.append(("match", label))
        return h.matched

    async def create_and_link(
        supabase: Any,
        *,
        user_id: str,
        payload: Any,
        activation_status: str | None = None,
        is_active: bool = False,
        active_limit: int | None = None,
    ) -> tuple[JobTarget, Any, bool]:
        h.events.append(("create_and_link", (payload.label, is_active, active_limit)))
        if h.create_error is not None:
            raise h.create_error
        return _target(label=payload.label), object(), h.was_created

    async def find_or_create(supabase: Any, payload: Any) -> tuple[JobTarget, bool]:
        h.events.append(("find_or_create", payload.label))
        return _target(label=payload.label), h.was_created

    async def link(
        supabase: Any, *, user_id: str, target_id: str, is_active: bool = True, **_: Any
    ) -> None:
        h.events.append(("link", (user_id, target_id, is_active)))
        if h.link_error is not None:
            raise h.link_error

    async def get(supabase: Any, target_id: str) -> JobTarget:
        return _target(id=target_id)

    async def choices(supabase: Any, user_id: str) -> list[dict[str, str]]:
        return []

    async def app_active(supabase: Any, target_id: str) -> JobTarget:
        h.events.append(("app_active", target_id))
        return _target(id=target_id)

    async def derive(llm: Any, *, jd_text: str, supabase: Any) -> tuple[DerivedTarget, LLMResult]:
        h.events.append(("derive", len(jd_text)))
        if h.derive_error is not None:
            raise h.derive_error
        return (
            DerivedTarget(scoring_profile=ScoringProfile(), search_keywords=["product manager"]),
            _llm_result(),
        )

    async def add_ref(*args: Any, **kwargs: Any) -> None:
        h.events.append(("reference_jd", kwargs.get("user_id")))

    async def update(supabase: Any, target_id: str, payload: TargetUpdate) -> JobTarget:
        h.events.append(("update", payload))
        h.updates.append(payload)
        return _target(id=target_id, activation_status=payload.activation_status or "idle")

    monkeypatch.setattr(router, "_get_job_posting_row", get_posting)
    monkeypatch.setattr(router, "_effective_active_target_cap_async", cap)
    monkeypatch.setattr(router, "_count_active_for_user_async", count_active)
    monkeypatch.setattr(from_input, "normalize_posting_title", normalize)
    monkeypatch.setattr(cost_log, "record_async", record)
    monkeypatch.setattr(router, "find_matching_target", match)
    monkeypatch.setattr(from_input, "create_and_link", create_and_link)
    monkeypatch.setattr(router, "_find_or_create_target_async", find_or_create)
    monkeypatch.setattr(router, "_link_user_to_target_async", link)
    monkeypatch.setattr(router, "_target_get", get)
    monkeypatch.setattr(router, "_active_target_choices", choices)
    monkeypatch.setattr(router, "_set_app_active_async", app_active)
    monkeypatch.setattr(router, "derive_profile_from_jd", derive)
    monkeypatch.setattr(router, "_add_reference_jd_async", add_ref)
    monkeypatch.setattr(router, "_update_target_async", update)
    return h


async def _call(user_id: str | None = "u-1") -> JobTarget:
    return await router.create_target_from_posting(
        "p-1", supabase=object(), llm=object(), user_id=user_id
    )


def _profile_writes(h: Harness) -> list[TargetUpdate]:
    return [u for u in h.updates if u.scoring_profile is not None]


# ---- Identity ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_decorated_title_is_canonicalized_before_matching_and_creating(h: Harness) -> None:
    """The normalizer sees the raw posting title; everything after it sees the
    canonical label. The raw title never reaches the catalog key."""
    target = await _call()

    assert h.named("normalize") == [POSTING_TITLE]
    assert h.named("match") == [CANONICAL]
    assert h.named("create_and_link") == [(CANONICAL, True, 3)]
    assert target.label == CANONICAL
    # The normalize call is billed like every other (the manual path always was).
    assert h.named("cost_log")[0] == from_input.NORMALIZE_TITLE_PURPOSE


@pytest.mark.asyncio
async def test_seed_reference_jd_is_attributed_to_the_creating_user(h: Harness) -> None:
    await _call()

    assert h.named("reference_jd") == ["u-1"]


@pytest.mark.asyncio
async def test_normalizer_failure_propagates_before_any_write(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1066 semantics, unchanged here: no raw-title fallback, nothing written."""

    async def broken(llm: Any, *, title: str, jd_text: str) -> tuple[NormalizedTitle, LLMResult]:
        raise LLMMalformedOutputError("blank label", reason="malformed_output")

    monkeypatch.setattr(from_input, "normalize_posting_title", broken)

    with pytest.raises(LLMMalformedOutputError):
        await _call()

    assert h.named("match") == []
    assert h.named("create_and_link") == []
    assert h.named("link") == []


# ---- Shared-catalog safety --------------------------------------------------


@pytest.mark.asyncio
async def test_match_attaches_the_caller_and_never_touches_the_shared_profile(h: Harness) -> None:
    h.matched = _target(id="t-existing")

    target = await _call()

    assert target.id == "t-existing"
    assert h.named("link") == [("u-1", "t-existing", True)]
    assert h.named("create_and_link") == []
    assert h.named("derive") == []
    assert h.named("reference_jd") == []
    assert _profile_writes(h) == []


@pytest.mark.asyncio
async def test_losing_the_exact_key_race_is_treated_as_a_match(h: Harness) -> None:
    """``was_created=False`` means a concurrent request inserted the row first.
    It is shared now, so this request must not derive over it."""
    h.was_created = False

    target = await _call()

    assert target.label == CANONICAL
    assert h.named("create_and_link") == [(CANONICAL, True, 3)]
    assert h.named("derive") == []
    assert h.named("reference_jd") == []
    assert _profile_writes(h) == []


@pytest.mark.asyncio
async def test_api_key_race_loser_is_not_derived_over_either(h: Harness) -> None:
    h.was_created = False

    await _call(user_id=None)

    assert h.named("find_or_create") == [CANONICAL]
    assert h.named("app_active") == ["t-new"]
    assert h.named("derive") == []


# ---- Active-target cap ------------------------------------------------------


@pytest.mark.asyncio
async def test_known_at_cap_is_refused_before_any_llm_call(h: Harness) -> None:
    h.cap = 1
    h.active_count = 1

    with pytest.raises(HTTPException) as exc:
        await _call()

    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "ACTIVE_LIMIT"
    assert h.named("normalize") == []
    assert h.named("cost_log") == []
    assert h.named("create_and_link") == []


@pytest.mark.asyncio
async def test_cap_rejection_at_the_atomic_write_is_a_409_with_no_derivation(h: Harness) -> None:
    """The preflight passed (a race), so the RPC rejected. Same payload, and
    the derivation was never paid for."""
    h.create_error = crud.ActiveTargetLimitError(current_count=3, limit=3)

    with pytest.raises(HTTPException) as exc:
        await _call()

    assert exc.value.status_code == 409
    assert exc.value.detail["limit"] == 3
    assert h.named("derive") == []
    assert h.named("reference_jd") == []


@pytest.mark.asyncio
async def test_api_key_caller_skips_the_cap_and_raises_app_active(h: Harness) -> None:
    await _call(user_id=None)

    assert h.named("cap") == []
    assert h.named("link") == []
    assert h.named("create_and_link") == []
    assert h.named("find_or_create") == [CANONICAL]
    assert h.named("app_active") == ["t-new"]
    assert from_input.NORMALIZE_TITLE_PURPOSE in h.named("cost_log")


# ---- Ordering ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_attach_happens_before_derivation_runs(h: Harness) -> None:
    await _call()

    order = h.order()
    assert order.index("cap") < order.index("normalize")
    assert order.index("create_and_link") < order.index("derive")


@pytest.mark.asyncio
async def test_short_description_still_canonicalizes_but_skips_derivation(h: Harness) -> None:
    h.description_html = "<p>Too short.</p>"

    target = await _call()

    assert h.named("normalize") == [POSTING_TITLE]
    assert h.named("derive") == []
    assert target.label == CANONICAL


# ---- Derivation failures, per the #1066 taxonomy ----------------------------


@pytest.mark.asyncio
async def test_provider_failure_marks_the_target_for_retry_and_still_returns(h: Harness) -> None:
    h.derive_error = LLMUpstreamUnavailableError()

    target = await _call()

    assert target.activation_status == "error"
    stamped = h.updates[-1]
    assert stamped.activation_status == "error"
    assert stamped.activation_error == ActivationError.PIPELINE_FAILED
    # The membership was written by the atomic create, so the retry is one
    # click away and nothing is orphaned.
    assert h.named("create_and_link") == [(CANONICAL, True, 3)]


@pytest.mark.asyncio
async def test_malformed_output_marks_the_target_for_retry(h: Harness) -> None:
    h.derive_error = LLMMalformedOutputError("truncated", reason="truncated")

    target = await _call()

    assert target.activation_status == "error"
    assert h.updates[-1].activation_error == ActivationError.PIPELINE_FAILED


@pytest.mark.asyncio
async def test_programming_error_propagates_after_stamping_with_no_profile_side_effects(
    h: Harness,
) -> None:
    """A ``TypeError`` (#1065's shape) must surface, never be logged past; the
    target it leaves behind is marked, not half-built."""
    h.derive_error = TypeError("unexpected keyword argument 'temperature'")

    with pytest.raises(TypeError):
        await _call()

    assert h.updates[-1].activation_status == "error"
    assert h.updates[-1].activation_error == ActivationError.PIPELINE_FAILED
    assert h.named("reference_jd") == []
    assert _profile_writes(h) == []
