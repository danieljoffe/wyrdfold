"""Unit tests for the from-url source-registration helper.

The helper classifies the pasted URL's ATS + filters dead boards (network
probe), then delegates the capped, attributed write to the
``register_source_from_url`` RPC. These cover both halves offline: the
Python-side branches (unclassified / dead-board / probe failure) and the
RPC delegation (args, status passthrough, response-shape normalization, and
never-raise resilience).
"""

from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.services import source_registration
from app.services.ats_detect import DetectResult

# A live Ashby board — the Hadrian case that motivated the feature.
LIVE = DetectResult(
    provider="ashby",
    board_token="hadrian-automation",
    company_name="Hadrian",
    job_count=12,
)
_URL = "https://jobs.ashbyhq.com/hadrian-automation/68286f52"


def _supabase(status: str = "registered") -> MagicMock:
    supabase = MagicMock()
    supabase.rpc.return_value.execute.return_value = MagicMock(data=status)
    return supabase


def _patch_detect(monkeypatch: pytest.MonkeyPatch, result: object) -> None:
    """Stub detect_ats to return ``result`` (a DetectResult or None), or to
    raise it when ``result`` is an exception instance."""

    async def _detect(_url: str) -> object:
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(source_registration, "detect_ats", _detect)


@pytest.mark.asyncio
async def test_non_ats_url_skipped_without_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-ATS URL (LinkedIn, custom careers page) is refused BEFORE the probe.

    detect_ats otherwise falls back to guessing a slug from the domain stem and
    probing every provider — ``linkedin.com/jobs/view/…`` coincidentally matches
    a Greenhouse ``linkedin`` board (verified live), which would register a wrong
    global source. The is_ats_url gate must short-circuit first.
    """
    probed = False

    async def _spy(_url: str) -> object:
        nonlocal probed
        probed = True
        return None

    monkeypatch.setattr(source_registration, "detect_ats", _spy)
    supabase = _supabase()

    outcome = await source_registration.register_source_from_url(
        supabase, user_id="u1", final_url="https://www.linkedin.com/jobs/view/123456"
    )

    assert outcome == "unclassified"
    assert probed is False  # gated out before the network probe
    supabase.rpc.assert_not_called()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://jobs.ashbyhq.com/hadrian-automation/68286f52", True),
        ("https://boards.greenhouse.io/acme", True),
        ("https://jobs.lever.co/acme/abc", True),
        ("https://acme.wd1.myworkdayjobs.com/careers", True),
        ("https://www.linkedin.com/jobs/view/123456", False),  # non-ATS host
        ("https://stripe.com/careers", False),  # company page, not its ATS board
        ("Hadrian", False),  # a bare company name is not a URL
    ],
)
def test_is_ats_url(url: str, expected: bool) -> None:
    from app.services.ats_detect import is_ats_url

    assert is_ats_url(url) is expected


@pytest.mark.asyncio
async def test_dead_board_not_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Classified but zero live postings → don't register a board we'd poll for
    nothing."""
    dead = DetectResult(
        provider="ashby", board_token="empty-co", company_name="Empty Co", job_count=0
    )
    _patch_detect(monkeypatch, dead)
    supabase = _supabase()

    outcome = await source_registration.register_source_from_url(
        supabase, user_id="u1", final_url="https://jobs.ashbyhq.com/empty-co/x"
    )

    assert outcome == "dead_board"
    supabase.rpc.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["registered", "linked", "already_owned", "cap_reached"])
async def test_live_board_delegates_to_rpc(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """A live board delegates to the RPC with the detected board + settings cap,
    and passes the server's status straight back."""
    _patch_detect(monkeypatch, LIVE)
    supabase = _supabase(status)

    outcome = await source_registration.register_source_from_url(
        supabase, user_id="u1", final_url=_URL
    )

    assert outcome == status
    name, params = supabase.rpc.call_args.args
    assert name == "register_source_from_url"
    assert params["p_user_id"] == "u1"
    assert params["p_provider"] == "ashby"
    assert params["p_board_token"] == "hadrian-automation"
    assert params["p_company_name"] == "Hadrian"
    assert params["p_cap"] == settings.source_registration_cap_per_user


@pytest.mark.asyncio
async def test_detect_failure_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A probe blow-up is swallowed (best-effort) — never raises, never writes."""
    _patch_detect(monkeypatch, RuntimeError("probe down"))
    supabase = _supabase()

    outcome = await source_registration.register_source_from_url(
        supabase, user_id="u1", final_url=_URL
    )

    assert outcome == "error"
    supabase.rpc.assert_not_called()


@pytest.mark.asyncio
async def test_rpc_failure_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """An RPC failure is swallowed too — the target the user created is unaffected."""
    _patch_detect(monkeypatch, LIVE)
    supabase = MagicMock()
    supabase.rpc.return_value.execute.side_effect = RuntimeError("rpc down")

    outcome = await source_registration.register_source_from_url(
        supabase, user_id="u1", final_url=_URL
    )

    assert outcome == "error"


@pytest.mark.asyncio
async def test_rpc_list_of_dict_shape_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    """postgrest may wrap the scalar as ``[{"register_source_from_url": "..."}]``."""
    _patch_detect(monkeypatch, LIVE)
    supabase = _supabase()
    supabase.rpc.return_value.execute.return_value = MagicMock(
        data=[{"register_source_from_url": "linked"}]
    )

    outcome = await source_registration.register_source_from_url(
        supabase, user_id="u1", final_url=_URL
    )

    assert outcome == "linked"


@pytest.mark.asyncio
async def test_rpc_unexpected_shape_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_detect(monkeypatch, LIVE)
    supabase = _supabase()
    supabase.rpc.return_value.execute.return_value = MagicMock(data={"weird": 1})

    outcome = await source_registration.register_source_from_url(
        supabase, user_id="u1", final_url=_URL
    )

    assert outcome == "error"
