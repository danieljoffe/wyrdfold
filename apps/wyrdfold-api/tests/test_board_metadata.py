"""Board-published metadata → column vocabularies (#846).

Ashby, Lever and SmartRecruiters state remote status, employment type and
country outright. We used to infer all of it with an LLM — twice, via two
paths that disagreed (#795: 229 prod contradictions on remote alone).

Sample values here are taken from LIVE payloads captured during the #846
audit, not invented, so a provider changing its spelling breaks a test rather
than silently degrading a filter.
"""

from __future__ import annotations

import pytest

from app.services.board_metadata import (
    board_columns,
    normalize_country,
    normalize_employment_type,
    normalize_remote,
)
from app.services.standard_job import StandardJob


def _job(**kw: object) -> StandardJob:
    base: dict[str, object] = {
        "external_id": "x",
        "title": "t",
        "location_name": None,
        "content": "",
        "posted_at": "",
        "absolute_url": "",
    }
    base.update(kw)
    return StandardJob(**base)  # type: ignore[arg-type]


# ---- employment type ---------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("FullTime", "full_time"),  # ashby employmentType
        ("Full-Time", "full_time"),  # lever categories.commitment
        ("permanent", "full_time"),  # smartrecruiters typeOfEmployment.id
        ("Part-Time", "part_time"),
        ("Contract", "contract"),
        ("Intern", "internship"),
        ("Seasonal", "temporary"),
    ],
)
def test_employment_type_spellings_collapse(raw: str, expected: str) -> None:
    assert normalize_employment_type(raw) == expected


@pytest.mark.parametrize("raw", ["", None, 42, "Fractional CTO", "???"])
def test_unrecognized_employment_type_says_nothing(raw: object) -> None:
    """Silence, not a guess — an unknown spelling must not be coerced into
    full_time, which is what would quietly mislabel every odd posting."""
    assert normalize_employment_type(raw) is None


# ---- country -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("United States", "US"),  # ashby addressCountry (a NAME)
        ("AR", "AR"),  # lever country (already ISO-2)
        ("de", "DE"),  # smartrecruiters location.country (lower-case)
    ],
)
def test_country_accepts_names_and_codes(raw: str, expected: str) -> None:
    assert normalize_country(raw) == expected


@pytest.mark.parametrize("raw", ["", "  ", None, 7, "Remote", "EMEA"])
def test_unrecognized_country_says_nothing(raw: object) -> None:
    assert normalize_country(raw) is None


# ---- remote ------------------------------------------------------------------


def test_boolean_remote_wins_over_descriptor() -> None:
    """Ashby sends both; the boolean is the field it models, the descriptor
    is a label."""
    assert normalize_remote(is_remote=True, workplace_type="OnSite") is True
    assert normalize_remote(is_remote=False, workplace_type="Remote") is False


def test_descriptor_used_when_no_boolean() -> None:
    """Lever publishes only workplaceType."""
    assert normalize_remote(workplace_type="remote") is True
    assert normalize_remote(workplace_type="onsite") is False


def test_hybrid_is_not_remote() -> None:
    """The /jobs remote filter means 'can I work remotely'. A hybrid role
    requires office presence, so it must not satisfy remote_only — this is
    the distinction #795 found the LLM getting wrong."""
    assert normalize_remote(workplace_type="Hybrid") is False
    # SmartRecruiters: remote=False + hybrid=True. The explicit hybrid flag is
    # strictly more specific than the bare false.
    assert normalize_remote(is_remote=False, is_hybrid=True) is False
    assert normalize_remote(is_remote=True, is_hybrid=True) is False


def test_unknown_remote_signal_says_nothing() -> None:
    assert normalize_remote() is None
    assert normalize_remote(workplace_type="Flexible") is None


# ---- the column spread -------------------------------------------------------


def test_board_columns_omits_what_the_board_didnt_say() -> None:
    """A silent board must leave the stored column untouched. Emitting
    ``{"is_remote": None}`` would blank a value another path established —
    the upsert writes every key present."""
    assert board_columns(_job()) == {}


def test_board_columns_keeps_an_explicit_false() -> None:
    """``is_remote=False`` is a fact ('this role is on-site'), not absence.
    A truthiness check here would silently drop every on-site posting."""
    assert board_columns(_job(is_remote=False)) == {"is_remote": False}


def test_board_columns_carries_what_the_board_did_say() -> None:
    cols = board_columns(_job(is_remote=True, employment_type="contract"))
    assert cols == {"is_remote": True, "employment_type": "contract"}


def test_board_columns_never_writes_country() -> None:
    """``jobs.country`` holds a DISPLAY vocabulary (``US``, ``UK`` — not
    ``GB``). #805 was exactly the bug where alpha-2 was matched against it and
    found nothing. The value is carried on StandardJob but must not land in
    that column until the vocabularies are deliberately reconciled."""
    cols = board_columns(_job(country="GB", is_remote=True))
    assert "country" not in cols
    assert cols == {"is_remote": True}
