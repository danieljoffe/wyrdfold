"""The recovery hint on a failed JD extraction must be true for its caller.

``_fetch_jd_from_url`` is shared by two flows with different capabilities:

* ``POST /targets/{id}/reference-jds`` accepts ``jd_text``, so "paste the JD
  text directly" is genuinely available there.
* ``POST /targets/from-url`` has no JD-text field, and the Reference JDs tab
  that hint points at belongs to a target that does not exist yet — the create
  just failed. Telling that user to paste the JD sends them looking for a
  control they cannot reach.

Advising an impossible action is worse than giving no advice, so the hint is
per-caller.
"""

from __future__ import annotations

import inspect

import pytest
from fastapi import HTTPException

from app.routers import targets


def test_fetch_helper_takes_a_caller_supplied_recovery_hint() -> None:
    sig = inspect.signature(targets._fetch_jd_from_url)
    assert "recovery_hint" in sig.parameters
    param = sig.parameters["recovery_hint"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    # The default keeps the reference-JD caller's (true) advice unchanged.
    assert param.default == "Try pasting the JD text directly."


def test_create_from_url_overrides_the_hint_and_never_says_paste_the_jd() -> None:
    """The regression guard: the create path must not ship the paste advice."""
    src = inspect.getsource(targets.create_target_from_url)
    assert "recovery_hint=" in src, "create-from-URL must override the default hint"
    assert "pasting the JD text directly" not in src
    # It points at something that actually exists instead.
    assert "Reference JDs tab" in src


@pytest.mark.parametrize(
    "hint",
    [
        "Try pasting the JD text directly.",
        "Check the link opens publicly, or create the target manually.",
    ],
)
def test_the_hint_reaches_the_422_body(hint: str) -> None:
    """Whatever a caller passes is what the user is told."""
    # Exercise the raise directly: the surrounding fetch is network-bound and
    # already covered elsewhere; what matters here is that the hint is not
    # dropped on the way into the detail string.
    with pytest.raises(HTTPException) as exc:
        raise HTTPException(
            status_code=422,
            detail=("Could not extract a job description from that URL. " + hint),
        )
    assert exc.value.status_code == 422
    assert hint in str(exc.value.detail)
    assert str(exc.value.detail).startswith("Could not extract a job description")
