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
    board_us_verdict,
    display_country,
    normalize_country,
    normalize_employment_type,
    normalize_remote,
)
from app.services.location_parse import parse_location
from app.services.qualification import is_us_location, positively_us_location
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


def test_location_remote_fills_the_gap_for_greenhouse_and_workday() -> None:
    """Neither publishes a remote flag, but both say it in the location string,
    and ``location_parse`` already reads that as board-stated ground truth."""
    loc = parse_location("Remote - US")
    assert loc.remote is True
    assert board_columns(_job(), loc) == {"is_remote": True}


def test_location_remote_false_asserts_nothing() -> None:
    """The asymmetry that matters: a location saying "Remote" proves remote,
    but a location not saying it proves nothing — plenty of remote roles just
    list an office. Writing False here would mislabel most of the corpus."""
    loc = parse_location("San Francisco, CA, US")
    assert loc.remote is False
    assert board_columns(_job(), loc) == {}


def test_a_board_that_states_remote_beats_the_location_string() -> None:
    """Ashby saying isRemote=False outranks a location that merely mentions
    remote — the provider models the field, the string just describes a place."""
    loc = parse_location("Remote - US")
    assert board_columns(_job(is_remote=False), loc) == {"is_remote": False}
    assert board_columns(_job(is_remote=True), loc) == {"is_remote": True}


def test_multi_location_remote_is_picked_up() -> None:
    """Greenhouse joins multiple locations with semicolons; the parser enriches
    remote from ANY segment, not just the first."""
    loc = parse_location("New York, NY, US; Remote, US")
    assert board_columns(_job(), loc) == {"is_remote": True}


# ---- country: the display vocabulary, and the US verdict ---------------------


def test_board_country_is_translated_to_the_display_vocabulary_not_iso() -> None:
    """#805 in one assertion: ``jobs.country`` is read by filters that send
    ``UK``, so a raw ``GB`` would match nothing. The board's ISO code has to be
    translated on the way in, never stored as-is."""
    assert normalize_country("United Kingdom") == "GB"  # precondition
    cols = board_columns(_job(country="GB", is_remote=True))
    assert cols == {"country": "UK", "is_remote": True}


def test_display_country_agrees_with_the_parser_for_every_code_it_knows() -> None:
    """The composed map may not invent a spelling. Every code it answers for
    must round-trip to the exact string ``location_parse`` produces — otherwise
    two writers put two spellings of one country in the same column."""
    assert display_country("GB") == parse_location("London, United Kingdom").country
    assert display_country("US") == parse_location("Austin, TX, United States").country
    assert display_country("CA") == parse_location("Toronto, Canada").country
    assert display_country("DE") == parse_location("Berlin, Germany").country


def test_display_country_says_nothing_for_a_country_it_cannot_spell() -> None:
    """``CH`` has no entry in the parser's display table. Inventing one would
    put a value in the column that no other writer could ever produce."""
    assert normalize_country("CH") == "CH"  # precondition: the board WAS read
    assert display_country("CH") is None
    assert board_columns(_job(country="CH")) == {}


def test_a_board_country_the_parser_missed_is_still_a_us_verdict() -> None:
    """Not being able to SPELL Switzerland has no bearing on whether the role
    is in the United States. The display column stays silent; the verdict does
    not."""
    job = _job(country="CH")
    assert board_columns(job) == {}
    assert board_us_verdict(job) is False


def test_board_us_verdict_reads_the_employers_own_answer() -> None:
    assert board_us_verdict(_job(country="US")) is True
    assert board_us_verdict(_job(country="DE")) is False


def test_silence_is_not_falsity() -> None:
    """Greenhouse and Workday publish no country. That must never become
    "not US" — it is the difference between an answer and no answer."""
    assert board_us_verdict(_job(country=None)) is None
    assert board_columns(_job(country=None)) == {}


def test_a_plainly_us_location_vetoes_a_foreign_board_country() -> None:
    """The #60 workstream-B veto, applied one step earlier. A posting whose
    postal address is abroad but which also lists a US office must not be
    pruned on the strength of the address alone — leave it NULL and let the
    grader decide."""
    job = _job(country="GB", location_name="New York, NY; London")
    assert positively_us_location(job.location_name) is True  # precondition
    assert board_us_verdict(job) is None


def test_a_vetoed_country_is_not_written_to_the_display_column_either() -> None:
    """ONE predicate for both writes. Filing a New York role under ``UK`` while
    refusing to prune it is the worst of both: a wrong /jobs country facet AND
    a disabled ``country = 'US'`` veto in the tagger, on exactly the
    multi-country class that veto exists for."""
    vetoed = _job(country="GB", location_name="New York, NY; London")
    assert board_us_verdict(vetoed) is None  # precondition
    assert "country" not in board_columns(vetoed)
    # Control: the identical board answer on an unambiguous location IS written,
    # so the assertion above cannot pass by the feature being dead.
    assert board_columns(_job(country="GB", location_name="London"))["country"] == "UK"


def test_the_veto_is_one_sided() -> None:
    """A US board country is recorded even when the location string is
    ambiguous — the veto exists to stop a foreign address hiding a US role,
    not to stop the employer confirming one."""
    job = _job(country="US", location_name="Remote")
    assert positively_us_location(job.location_name) is False  # precondition
    assert board_us_verdict(job) is True


# ---- the code that is not a country, and the country that is the US ---------


@pytest.mark.parametrize("state", ["TX", "NY", "FL", "OH", "WA", "MI"])
def test_a_us_state_code_that_is_not_an_iso_country_is_refused(state: str) -> None:
    """ "Two letters and alphabetic" is not a country. This value now drives a
    ONE-WAY archive, so a board putting a state code in its country field would
    have pruned a US role outright."""
    assert normalize_country(state) is None
    assert board_us_verdict(_job(country=normalize_country(state))) is None


@pytest.mark.parametrize(
    ("code", "us_location"),
    [
        ("CA", "San Diego"),
        ("CO", "Denver"),
        ("GA", "Atlanta"),
        ("IL", "Chicago"),
        ("MA", "Boston"),
    ],
)
def test_a_state_ambiguous_code_will_not_prune_a_location_that_reads_as_us(
    code: str, us_location: str
) -> None:
    """``CA`` is Canada in ISO and California in an address, and the archive is
    one-way. When the location's own deterministic parse says US, withhold —
    even though ``positively_us_location`` (which needs an explicit marker or a
    "City, ST" form) would not have caught these bare metro names."""
    job = _job(country=code, location_name=us_location)
    assert positively_us_location(us_location) is False  # precondition: the OLD veto misses
    assert parse_location(us_location).country == "US"  # precondition: the parse sees US
    assert board_us_verdict(job) is None
    assert "country" not in board_columns(job)


@pytest.mark.parametrize(
    ("code", "location"),
    [("CA", "Ontario - Remote"), ("DE", "Stuttgart"), ("MT", "Valetta"), ("IN", "Ahmedabad")],
)
def test_a_state_ambiguous_code_still_prunes_when_nothing_says_us(code: str, location: str) -> None:
    """The guard must not cost the signal. All four are real live postings; the
    ambiguous codes carry 883 of the 4,285 prunes in the fleet sample, so
    dropping them wholesale would forfeit a fifth of the pruning."""
    assert board_us_verdict(_job(country=code, location_name=location)) is False


@pytest.mark.parametrize("code", ["PR", "GU", "VI", "AS", "MP", "UM"])
def test_us_territories_are_the_united_states(code: str) -> None:
    """Puerto Rico and Guam carry their own ISO codes and were being archived as
    foreign — a live Lever posting located "American Samoa" was in the sample."""
    assert board_us_verdict(_job(country=code, location_name="Remote")) is True


def test_the_british_virgin_islands_are_not_the_us() -> None:
    """``VG`` next to ``VI`` is exactly the pair a copied-in territory list gets
    wrong."""
    assert board_us_verdict(_job(country="VG", location_name="Remote")) is False


def test_the_ambiguous_residue_the_l1_gate_cannot_judge() -> None:
    """The whole point of this signal. ``is_us_location`` ADMITS these strings
    (no known non-US hint), so ingest keeps them and — since tagging went lazy
    — they would sit at ``is_us = NULL`` and stay publicly visible. The board
    already knows the answer."""
    for location, country in (
        ("Cluj-Napoca", "RO"),
        ("Utrecht", "NL"),
        ("Hybrid", "SG"),
        ("Remote", "DE"),
    ):
        job = _job(location_name=location, country=country)
        assert is_us_location(location) is True, location  # precondition: ingest keeps it
        assert board_us_verdict(job) is False, location
