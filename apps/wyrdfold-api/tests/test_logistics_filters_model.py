"""Tests for the LogisticsFilters model (groundwork for logistics chips).

Schema-only at this stage. The Phase 2 grader doesn't emit these
fields yet; this test suite locks the shape so the follow-up prompt
PR has a clear contract to target.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.logistics import LogisticsFilters


def test_defaults_are_unspecified() -> None:
    """The grader should produce a defaulted row even when the JD says
    nothing useful — never a missing column. ``unspecified`` is the
    sentinel for "no signal" so consumers can distinguish from null."""
    f = LogisticsFilters()
    assert f.remote_status == "unspecified"
    assert f.salary_min is None
    assert f.salary_max is None
    assert f.salary_currency is None
    assert f.salary_unit is None
    assert f.location_city is None
    assert f.location_country is None
    assert f.has_any_signal() is False


def test_rejects_unknown_remote_status() -> None:
    """Enum is closed: extending it is intentional, not silent."""
    with pytest.raises(ValidationError):
        LogisticsFilters(remote_status="maybe")  # type: ignore[arg-type]


def test_rejects_negative_salary() -> None:
    """A negative salary is always a parse bug, not a legitimate value."""
    with pytest.raises(ValidationError):
        LogisticsFilters(salary_min=-1)


def test_rejects_unknown_salary_unit() -> None:
    with pytest.raises(ValidationError):
        LogisticsFilters(salary_unit="month")  # type: ignore[arg-type]


def test_has_any_signal_true_when_remote_known() -> None:
    f = LogisticsFilters(remote_status="remote")
    assert f.has_any_signal() is True


def test_has_any_signal_true_when_salary_floor_known() -> None:
    f = LogisticsFilters(salary_min=150000)
    assert f.has_any_signal() is True


def test_has_any_signal_true_when_location_known() -> None:
    f = LogisticsFilters(location_country="US")
    assert f.has_any_signal() is True


def test_full_shape_round_trips_through_dict() -> None:
    """Important for the jsonb write/read path: a fully-populated row
    should serialize to a dict the DB can store and parse back into an
    identical model."""
    f = LogisticsFilters(
        remote_status="hybrid",
        salary_min=150_000,
        salary_max=180_000,
        salary_currency="USD",
        salary_unit="year",
        location_city="San Francisco",
        location_country="US",
    )
    roundtripped = LogisticsFilters.model_validate(f.model_dump())
    assert roundtripped == f


def test_country_field_caps_short_codes() -> None:
    """ISO country codes are 2 or 3 chars; the field allows up to 4 for
    legacy /  special-case codes but should reject sprawling free-text."""
    LogisticsFilters(location_country="US")
    LogisticsFilters(location_country="USA")
    with pytest.raises(ValidationError):
        LogisticsFilters(location_country="Republic of Sprawling Free Text")


def test_recognized_country_names_normalize_to_alpha2() -> None:
    """#693: the grader occasionally writes the NAME instead of the code.

    Because ``complete_json`` validates the whole payload at once, that used to
    fail the ENTIRE fit-score call — observed live in prod on 'India'. A
    recognized name now normalizes instead, so one slip on an optional field
    cannot take out the response.
    """
    assert LogisticsFilters(location_country="India").location_country == "IN"
    assert LogisticsFilters(location_country="Canada").location_country == "CA"
    # Case- and whitespace-insensitive; multi-word names included.
    assert LogisticsFilters(location_country="  costa rica ").location_country == "CR"
    assert LogisticsFilters(location_country="UNITED STATES").location_country == "US"
    # The long form the old test pinned as a rejection is a KNOWN country, so
    # it now normalizes rather than failing. Rejecting it protected the
    # column's vocabulary; normalizing protects it too, and keeps the call.
    assert LogisticsFilters(location_country="United States of America").location_country == "US"


def test_codes_pass_through_untouched() -> None:
    """Normalization must not disturb the vocabulary already in the column —
    all 2,027 populated prod rows are alpha-2 codes."""
    for code in ("US", "GB", "IN", "DE", "BR", "ZA"):
        assert LogisticsFilters(location_country=code).location_country == code


def test_unrecognized_long_values_still_fail_loudly() -> None:
    """Normalizing known countries is not a licence to accept free text — an
    unrecognized sprawling string is a real problem and must stay loud."""
    for junk in ("Somewhere over the rainbow", "Atlantis Federation", "n/a — see posting"):
        with pytest.raises(ValidationError):
            LogisticsFilters(location_country=junk)
