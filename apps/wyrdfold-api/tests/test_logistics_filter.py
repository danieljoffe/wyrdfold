"""/jobs logistics filters (#86) — the post-fetch filter over the grader's
``scores.logistics_filters`` data.

Semantics (plan-wyrdfold-logistics-chips.md): ``remote_only`` and ``min_salary``
are STRICT (an unknown value is dropped — the user opted into a hard filter),
``country`` is LENIENT (a missing country anchor still passes, since remote roles
often have none). A row with NO logistics data at all is therefore dropped by
remote_only / min_salary but kept by country.
"""

from __future__ import annotations

from app.routers.jobs import (
    _apply_logistics_filter,
    _logistics_passes,
    _LogisticsFilter,
)


def _row(**logistics: object) -> dict[str, object]:
    return {"id": "j", "logistics_filters": dict(logistics) if logistics else None}


# ---- active flag -----------------------------------------------------------


def test_filter_inactive_by_default_is_passthrough() -> None:
    f = _LogisticsFilter()
    assert f.active is False
    rows = [_row(remote_status="onsite"), _row()]
    assert _apply_logistics_filter(rows, f) == rows


def test_active_when_any_param_set() -> None:
    assert _LogisticsFilter(remote_only=True).active is True
    assert _LogisticsFilter(min_salary=100_000).active is True
    assert _LogisticsFilter(country="US").active is True


# ---- remote_only (STRICT) --------------------------------------------------


def test_remote_only_keeps_remote_drops_everything_else() -> None:
    f = _LogisticsFilter(remote_only=True)
    assert _logistics_passes({"remote_status": "remote"}, f) is True
    for status in ("hybrid", "onsite", "unspecified"):
        assert _logistics_passes({"remote_status": status}, f) is False
    # No logistics data at all → dropped (strict; user asked for remote).
    assert _logistics_passes(None, f) is False
    assert _logistics_passes({}, f) is False


# ---- min_salary (STRICT) ---------------------------------------------------


def test_min_salary_keeps_at_or_above_drops_below_and_unknown() -> None:
    f = _LogisticsFilter(min_salary=150_000)
    assert _logistics_passes({"salary_max": 150_000}, f) is True  # inclusive
    assert _logistics_passes({"salary_max": 200_000}, f) is True
    assert _logistics_passes({"salary_max": 120_000}, f) is False
    assert _logistics_passes({"salary_max": None}, f) is False  # undisclosed → drop
    assert _logistics_passes(None, f) is False


# ---- country (LENIENT) -----------------------------------------------------


def test_country_matches_case_insensitive_and_null_passes() -> None:
    f = _LogisticsFilter(country="US")
    assert _logistics_passes({"location_country": "US"}, f) is True
    assert _logistics_passes({"location_country": "us"}, f) is True  # case-insensitive
    assert _logistics_passes({"location_country": "CA"}, f) is False  # mismatch
    # Absent country anchor passes (lenient — a remote role may have none).
    assert _logistics_passes({"location_country": None}, f) is True
    assert _logistics_passes(None, f) is True


# ---- composition -----------------------------------------------------------


def test_all_three_compose_as_conjunction() -> None:
    f = _LogisticsFilter(remote_only=True, min_salary=150_000, country="US")
    ok = {"remote_status": "remote", "salary_max": 180_000, "location_country": "US"}
    assert _logistics_passes(ok, f) is True
    # Fails any single leg → dropped.
    assert _logistics_passes({**ok, "remote_status": "hybrid"}, f) is False
    assert _logistics_passes({**ok, "salary_max": 100_000}, f) is False
    assert _logistics_passes({**ok, "location_country": "CA"}, f) is False
    # country still lenient inside the conjunction (null country leg passes).
    assert _logistics_passes({**ok, "location_country": None}, f) is True


def test_apply_filters_a_list() -> None:
    f = _LogisticsFilter(remote_only=True, min_salary=150_000)
    rows = [
        {"id": "keep", "logistics_filters": {"remote_status": "remote", "salary_max": 160_000}},
        {"id": "low", "logistics_filters": {"remote_status": "remote", "salary_max": 90_000}},
        {"id": "onsite", "logistics_filters": {"remote_status": "onsite", "salary_max": 200_000}},
        {"id": "empty", "logistics_filters": None},
    ]
    assert [p["id"] for p in _apply_logistics_filter(rows, f)] == ["keep"]
