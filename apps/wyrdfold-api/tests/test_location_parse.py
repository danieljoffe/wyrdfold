"""Regression corpus for ``parse_location`` (#518 location normalization).

Every input below is a VERBATIM ``jobs.location`` value sampled from prod
(2026-07-28, ordered roughly by row count) — the same corpus discipline as
salary extraction: grow this table with every real-world miss, never shrink
it. Expected values are (city, state, country, remote).
"""

import pytest

from app.services.location_parse import LocationParts, parse_location

# fmt: off
CORPUS: list[tuple[str, tuple[str | None, str | None, str | None, bool]]] = [
    # -- bare metros / cities ------------------------------------------------
    ("San Francisco",                            ("San Francisco", "CA", "US", False)),
    ("New York City",                            ("New York", "NY", "US", False)),
    ("New York",                                 ("New York", "NY", "US", False)),
    ("Chicago",                                  ("Chicago", "IL", "US", False)),
    ("Sunnyvale",                                ("Sunnyvale", "CA", "US", False)),
    ("London",                                   ("London", None, "UK", False)),
    ("Manila",                                   ("Manila", None, "Philippines", False)),
    ("Gurugram",                                 ("Gurugram", None, "India", False)),
    # -- City, ST ------------------------------------------------------------
    ("San Francisco, CA",                        ("San Francisco", "CA", "US", False)),
    ("New York, NY",                             ("New York", "NY", "US", False)),
    ("Washington, DC",                           ("Washington", "DC", "US", False)),
    ("Boston, MA",                               ("Boston", "MA", "US", False)),
    ("Hawthorne, CA",                            ("Hawthorne", "CA", "US", False)),
    ("Austin, TX",                               ("Austin", "TX", "US", False)),
    ("New York City, NY",                        ("New York", "NY", "US", False)),
    ("Redmond, WA",                              ("Redmond", "WA", "US", False)),
    ("Chicago, IL",                              ("Chicago", "IL", "US", False)),
    ("Seattle, WA",                              ("Seattle", "WA", "US", False)),
    ("Redlands, CA",                             ("Redlands", "CA", "US", False)),
    ("Foster City, CA",                          ("Foster City", "CA", "US", False)),
    ("Mountain View, CA",                        ("Mountain View", "CA", "US", False)),
    ("Bastrop, TX",                              ("Bastrop", "TX", "US", False)),
    ("Palo Alto, CA",                            ("Palo Alto", "CA", "US", False)),
    ("Los Angeles, CA",                          ("Los Angeles", "CA", "US", False)),
    # -- City, State (full name) --------------------------------------------
    ("New York, New York",                       ("New York", "NY", "US", False)),
    ("San Francisco, California",                ("San Francisco", "CA", "US", False)),
    ("Dallas, Texas",                            ("Dallas", "TX", "US", False)),
    # -- City, State, Country ------------------------------------------------
    ("Costa Mesa, California, United States",    ("Costa Mesa", "CA", "US", False)),
    ("San Francisco, California, United States", ("San Francisco", "CA", "US", False)),
    ("New York, New York, United States",        ("New York", "NY", "US", False)),
    ("Boston, Massachusetts, United States",     ("Boston", "MA", "US", False)),
    ("Seattle, Washington, United States",       ("Seattle", "WA", "US", False)),
    ("New York, New York, USA",                  ("New York", "NY", "US", False)),
    ("San Francisco, CA, USA",                   ("San Francisco", "CA", "US", False)),
    # -- glued state+country (missing comma) ---------------------------------
    ("San Mateo, CA United States",              ("San Mateo", "CA", "US", False)),
    # -- country only --------------------------------------------------------
    ("US",                                       (None, None, "US", False)),
    ("United States",                            (None, None, "US", False)),
    ("UK",                                       (None, None, "UK", False)),
    # -- non-US city + country ----------------------------------------------
    ("London, UK",                               ("London", None, "UK", False)),
    ("London, gb",                               ("London", None, "UK", False)),
    ("Toronto, Ontario",                         ("Toronto", None, "Canada", False)),
    # -- remote variants -----------------------------------------------------
    ("Remote",                                   (None, None, None, True)),
    ("Remote - US",                              (None, None, "US", True)),
    ("Remote - USA",                             (None, None, "US", True)),
    ("Remote - United States",                   (None, None, "US", True)),
    ("United States - Remote",                   (None, None, "US", True)),
    ("US Remote",                                (None, None, "US", True)),
    ("Remote US",                                (None, None, "US", True)),
    ("Remote, US",                               (None, None, "US", True)),
    ("Remote, USA",                              (None, None, "US", True)),
    ("United States (Remote)",                   (None, None, "US", True)),
    ("USA | Remote",                             (None, None, "US", True)),
    ("United States | Remote",                   (None, None, "US", True)),
    ("Remote - San Francisco, CA",               ("San Francisco", "CA", "US", True)),
    # -- workday CC-RR-City --------------------------------------------------
    ("US-CA-Menlo Park",                         ("Menlo Park", "CA", "US", False)),
    ("IT-FI-FLORENCE-VIA FELICE MATTEUCCI 2",    ("Florence", "FI", "Italy", False)),
    # -- office-suffix noise -------------------------------------------------
    ("London - The River Building HQ",           ("London", None, "UK", False)),
    ("Los Angeles, CA - Downtown",               ("Los Angeles", "CA", "US", False)),
    # -- multi-location strings (primary segment wins) -----------------------
    ("San Francisco, CA, US; Remote, US",        ("San Francisco", "CA", "US", True)),
    ("San Francisco, CA | New York City, NY",    ("San Francisco", "CA", "US", False)),
    ("San Francisco, CA | New York City, NY | Seattle, WA",
                                                 ("San Francisco", "CA", "US", False)),
    ("San Francisco, CA • New York, NY • United States",
                                                 ("San Francisco", "CA", "US", False)),
    ("Mountain View, California; San Francisco, California",
                                                 ("Mountain View", "CA", "US", False)),
    ("New York, NY; San Francisco, CA",          ("New York", "NY", "US", False)),
    # -- unparseable → all-None, raw display falls back ----------------------
    ("2 Locations",                              (None, None, None, False)),
    ("3 Locations",                              (None, None, None, False)),
    ("Hybrid",                                   (None, None, None, False)),
    ("Asia",                                     (None, None, None, False)),
]
# fmt: on


@pytest.mark.parametrize(("raw", "expected"), CORPUS, ids=[c[0] for c in CORPUS])
def test_prod_corpus(raw: str, expected: tuple[str | None, str | None, str | None, bool]) -> None:
    got = parse_location(raw)
    assert (got.city, got.state, got.country, got.remote) == expected


def test_none_and_empty() -> None:
    assert parse_location(None) == LocationParts(None, None, None, False)
    assert parse_location("   ") == LocationParts(None, None, None, False)


def test_state_abbrev_requires_uppercase() -> None:
    """Lowercase two-letter tokens are ISO country codes or prose, never
    states — 'London, gb' must not become Georgia."""
    got = parse_location("London, gb")
    assert (got.state, got.country) == (None, "UK")


def test_de_token_is_delaware_not_germany() -> None:
    got = parse_location("Wilmington, DE")
    assert (got.city, got.state, got.country) == ("Wilmington", "DE", "US")


def test_vancouver_wa_stays_us() -> None:
    """Explicit state tokens beat the metro map's country."""
    got = parse_location("Vancouver, WA")
    assert (got.city, got.state, got.country) == ("Vancouver", "WA", "US")


def test_campus_junk_yields_no_parts() -> None:
    got = parse_location("Washington University Medical Campus")
    assert got.is_empty


def test_remote_hybrid_wording_is_not_remote() -> None:
    got = parse_location("Hybrid")
    assert got.remote is False


# -- corpus growth: post-backfill prod gap sample (2026-07-29) ---------------
# fmt: off
CORPUS_2 = [
    ("Detroit",                                  ("Detroit", "MI", "US", False)),
    ("Kolkata, in",                              ("Kolkata", None, "India", False)),
    ("Chennai",                                  ("Chennai", None, "India", False)),
    ("Gurgaon",                                  ("Gurgaon", None, "India", False)),
    ("Quezon City",                              ("Quezon City", None, "Philippines", False)),
    ("Kyiv",                                     ("Kyiv", None, "Ukraine", False)),
    ("Budapest",                                 ("Budapest", None, "Hungary", False)),
    ("Kuala Lumpur",                             ("Kuala Lumpur", None, "Malaysia", False)),
    ("Belfast",                                  ("Belfast", None, "UK", False)),
    ("Bogota",                                   ("Bogota", None, "Colombia", False)),
    ("San Carlos  - Hybrid",                     ("San Carlos", "CA", "US", False)),
    # Comma-less suffix forms (real prod strings).
    ("New York New York United States",          ("New York", "NY", "US", False)),
    ("Heredia  Costa Rica",                      ("Heredia", None, "Costa Rica", False)),
    # Suffix rule must NOT swallow junk: no country suffix → unparsed.
    ("Washington University Medical Campus",     (None, None, None, False)),
    ("New York Office",                          (None, None, None, False)),
    ("Oklahoma County",                          (None, None, None, False)),
    ("Home based - Worldwide",                   (None, None, None, False)),
    ("Distributed",                              (None, None, None, False)),
]
# fmt: on


@pytest.mark.parametrize(("raw", "expected"), CORPUS_2, ids=[c[0] for c in CORPUS_2])
def test_prod_corpus_growth(
    raw: str, expected: tuple[str | None, str | None, str | None, bool]
) -> None:
    got = parse_location(raw)
    assert (got.city, got.state, got.country, got.remote) == expected
