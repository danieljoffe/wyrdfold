"""Every country the UI offers must be resolvable by the filter (#805).

The dropdown sends ISO alpha-2 and `jobs.country` stores a display form, so an
option whose code `canonical_country` cannot resolve is a menu entry that
silently returns nothing — which is precisely the bug #805 was about, just
reintroduced one option at a time.

This reads the ACTUAL option list out of the frontend rather than restating it,
so adding an unresolvable country to the menu fails here instead of in prod.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.services.location_parse import canonical_country

_FILTER_TSX = (
    Path(__file__).resolve().parents[2] / "wyrdfold/src/app/(app)/jobs/JobsFilter.tsx"
)


def _ui_country_options() -> list[tuple[str, str]]:
    block = re.search(
        r"const COUNTRY_OPTIONS[^=]*=\s*\[(.*?)\];", _FILTER_TSX.read_text(), re.S
    )
    assert block, "COUNTRY_OPTIONS not found — did the filter move?"
    return [
        (m.group(1), m.group(2))
        for m in re.finditer(
            r"\{\s*value:\s*'([^']*)',\s*label:\s*'([^']*)'\s*\}", block.group(1)
        )
    ]


def test_the_scan_found_the_real_menu() -> None:
    """Without this the contract below passes trivially on an empty list."""
    opts = _ui_country_options()
    assert len(opts) > 5, f"only parsed {len(opts)} options"
    assert ("", "Any country") in opts


def test_every_offered_country_resolves() -> None:
    unresolvable = [
        (value, label)
        for value, label in _ui_country_options()
        if value and canonical_country(value) != canonical_country(label)
    ]
    assert not unresolvable, (
        "These menu options cannot match their own data — the filter sends the "
        f"code and storage holds the label: {unresolvable}"
    )


def test_offered_countries_are_within_the_api_length_cap() -> None:
    """`GET /jobs` caps `country` at 4 chars; a longer value 422s."""
    too_long = [v for v, _ in _ui_country_options() if len(v) > 4]
    assert not too_long, f"country codes exceed the API's max_length=4: {too_long}"
