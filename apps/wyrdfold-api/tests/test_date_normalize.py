"""Tests for ATS posting-date normalization (prod bug: 22007/22008).

These reproduce the exact raw values that failed the live poll upsert
against the ``greenhouse_updated_at`` (``timestamptz``) column:

- relative Workday strings: "Posted Today", "Posted 5 Days Ago",
  "Posted 11 Days Ago"
- a 13-digit millisecond epoch put into a timestamp: "1779198175584"

The contract: parse to an ISO-8601 UTC string Postgres accepts, or
return ``None`` (written as SQL NULL). Never raise — an unparseable date
must NULL out, never fail the whole batch upsert (and thus the source).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.date_normalize import normalize_posted_at


def _today() -> datetime:
    now = datetime.now(UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _parse(value: str | None) -> datetime:
    assert value is not None
    return datetime.fromisoformat(value)


# ---- The exact prod-failing inputs ----------------------------------------


def test_posted_today_parses_to_today() -> None:
    result = _parse(normalize_posted_at("Posted Today"))
    assert result.date() == _today().date()
    assert result.tzinfo is not None  # tz-aware, so Postgres reads UTC


def test_posted_yesterday_parses_to_yesterday() -> None:
    result = _parse(normalize_posted_at("Posted Yesterday"))
    assert result.date() == (_today() - timedelta(days=1)).date()


@pytest.mark.parametrize(
    ("raw", "days"),
    [
        ("Posted 5 Days Ago", 5),
        ("Posted 11 Days Ago", 11),
        ("Posted 1 Day Ago", 1),
    ],
)
def test_posted_n_days_ago_parses_to_today_minus_n(raw: str, days: int) -> None:
    result = _parse(normalize_posted_at(raw))
    assert result.date() == (_today() - timedelta(days=days)).date()


def test_ms_epoch_13_digits_parses_to_seconds() -> None:
    # The prod 22008 value. 1779198175584 ms == 2026-05-19T... UTC.
    result = _parse(normalize_posted_at("1779198175584"))
    expected = datetime.fromtimestamp(1779198175584 / 1000.0, tz=UTC)
    assert result == expected
    # And it is well inside Postgres' representable timestamptz range
    # (the bare 1779198175584 *seconds* would have been year ~58346).
    assert result.year == expected.year


# ---- The "never raise; NULL on unparseable" guarantee ---------------------


@pytest.mark.parametrize(
    "raw",
    [
        "Posted 5 Days Ago",
        "Posted Today",
        "Posted 11 Days Ago",
        "1779198175584",
        "garbage",
        "",
        None,
        "Posted sometime last quarter",
        "N/A",
        "12",  # too few digits to be an epoch
        12345,  # a too-small int
    ],
)
def test_never_raises_returns_str_or_none(raw: object) -> None:
    """Every input the poller can hand us either parses to a string or
    NULLs out — and the call itself never raises."""
    result = normalize_posted_at(raw)
    assert result is None or isinstance(result, str)
    if isinstance(result, str):
        # Whatever we return must be Postgres-parseable ISO-8601.
        datetime.fromisoformat(result)


def test_unparseable_relative_string_is_null_not_crash() -> None:
    assert normalize_posted_at("Posted sometime last quarter") is None


def test_empty_and_none_are_null() -> None:
    assert normalize_posted_at("") is None
    assert normalize_posted_at(None) is None
    assert normalize_posted_at("   ") is None


# ---- ISO + happy-path passthrough (don't regress the common case) ---------


def test_iso_with_offset_passes_through() -> None:
    result = _parse(normalize_posted_at("2026-04-01T12:00:00+00:00"))
    assert result == datetime(2026, 4, 1, 12, 0, tzinfo=UTC)


def test_iso_with_trailing_z_passes_through() -> None:
    result = _parse(normalize_posted_at("2026-04-01T12:00:00Z"))
    assert result == datetime(2026, 4, 1, 12, 0, tzinfo=UTC)


def test_iso_date_only_is_assumed_utc() -> None:
    result = _parse(normalize_posted_at("2026-04-01"))
    assert result == datetime(2026, 4, 1, 0, 0, tzinfo=UTC)


def test_naive_iso_datetime_is_assumed_utc() -> None:
    # A fetcher that hands us a naive ISO string must still come back
    # tz-aware so Postgres stores the right instant.
    result = _parse(normalize_posted_at("2026-04-01T12:00:00"))
    assert result.tzinfo is not None
    assert result == datetime(2026, 4, 1, 12, 0, tzinfo=UTC)


def test_seconds_epoch_10_digits_parses() -> None:
    # A plain 10-digit second-epoch (Lever-style createdAt is ms, but be
    # robust): 1700000000 == 2023-11-14 UTC.
    result = _parse(normalize_posted_at("1700000000"))
    assert result == datetime.fromtimestamp(1700000000, tz=UTC)


def test_datetime_input_normalized_to_utc_iso() -> None:
    aware = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
    assert normalize_posted_at(aware) == aware.isoformat()
