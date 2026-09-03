"""US-location detection: ISO/state collisions and the bare ``US`` code.

Two defects, both found while measuring deterministic ``is_us`` coverage
against production, and both fixed here. Every location string used below is a
REAL one taken from the ``jobs`` table, not an invented example.
"""

from app.services.qualification.heuristics import (
    is_us_location,
    positively_us_location,
)

# ---------------------------------------------------------------------------
# Defect 1 — a foreign city with a state-colliding ISO code read as US
# ---------------------------------------------------------------------------


class TestForeignCityStateCollision:
    """``positively_us_location`` treats a trailing ", XX" as a US state. Its
    docstring claimed the no-foreign-hint clause covered the collisions and
    named ``Bangalore, IN`` as proof — but that clause only works for cities
    the hint list KNOWS. It did not know Chennai, so ``Chennai, IN`` read as
    Indiana and VETOED the non-US archive for an Indian posting."""

    def test_chennai_in_is_not_a_us_veto(self) -> None:
        assert positively_us_location("Chennai, IN") is False

    def test_ahmedabad_in_is_not_a_us_veto(self) -> None:
        assert positively_us_location("Ahmedabad, IN") is False

    def test_chennai_tn_is_not_a_us_veto(self) -> None:
        """TN is Tamil Nadu as well as Tennessee — the same collision one
        state over, and a real production string."""
        assert positively_us_location("Chennai, TN") is False

    def test_tbilisi_georgia_is_not_a_us_veto(self) -> None:
        assert positively_us_location("Tbilisi, Georgia") is False

    def test_stuttgart_de_is_not_a_us_veto(self) -> None:
        assert positively_us_location("Stuttgart, DE") is False

    def test_the_hint_the_docstring_already_claimed(self) -> None:
        """Control: the case the docstring cited was genuinely covered."""
        assert positively_us_location("Bangalore, IN") is False


class TestGenuineUsStatesKeepTheirVeto:
    """The veto exists so a high-confidence non-US verdict cannot archive a
    real US posting. Withdrawing it from genuine US cities would trade a
    precision bug for a job-losing one, so these must not regress."""

    def test_indianapolis_keeps_its_veto(self) -> None:
        assert positively_us_location("Indianapolis, IN") is True

    def test_atlanta_keeps_its_veto(self) -> None:
        assert positively_us_location("Atlanta, GA") is True

    def test_wilmington_keeps_its_veto(self) -> None:
        assert positively_us_location("Wilmington, DE") is True

    def test_panama_city_florida_keeps_its_veto(self) -> None:
        """ "panama" is deliberately NOT a hint: Panama City, FL is real."""
        assert positively_us_location("Panama City, FL") is True

    def test_hamburg_new_york_keeps_its_veto(self) -> None:
        """ "hamburg" is deliberately NOT a hint: Hamburg, NY/PA are real."""
        assert positively_us_location("Hamburg, NY") is True


class TestUsPlaceNamesAreNotRejectedAtIngest:
    """A hint does double duty: it also REJECTS at the ingest gate. Adding a
    name that is also a US place would silently stop ingesting real US
    listings, so the dangerous ones were left out on purpose."""

    def test_the_us_state_georgia_still_ingests(self) -> None:
        assert is_us_location("Atlanta, Georgia") is True

    def test_morocco_indiana_still_ingests(self) -> None:
        assert is_us_location("Morocco, Indiana") is True

    def test_sudan_texas_still_ingests(self) -> None:
        assert is_us_location("Sudan, Texas") is True

    def test_bogota_new_jersey_still_ingests(self) -> None:
        """Unaccented "bogota" is excluded for exactly this row; the accented
        "bogotá" is the one that got added."""
        assert is_us_location("Bogota, New Jersey") is True


# ---------------------------------------------------------------------------
# Defect 2 — the bare alpha-2 ``US`` was not a recognised marker
# ---------------------------------------------------------------------------


class TestBareUsCountryCode:
    """``_US_COUNTRY_RE`` accepted usa / u.s. / united states but not ``US``.
    Workday publishes location as ``US-CA-Menlo Park``; those postings carried
    no positive US marker and so lost the archive veto meant to protect them.
    Measured: 4,551 real rows gain a US marker from this."""

    def test_remote_us(self) -> None:
        assert positively_us_location("Remote - US") is True

    def test_bare_us(self) -> None:
        assert positively_us_location("US") is True

    def test_workday_style_prefix(self) -> None:
        assert positively_us_location("US-CA-Menlo Park") is True

    def test_workday_long_form(self) -> None:
        assert positively_us_location("US - Headquarters - Maryland - Columbia") is True

    def test_lowercase_us_is_the_pronoun_not_the_country(self) -> None:
        """The match is CASE-SENSITIVE on purpose. A case-insensitive \\bus\\b
        would read prose as a US marker, and this marker VETOES a non-US
        archive — so a false positive keeps foreign postings in the catalog."""
        # NB no foreign city here on purpose. An earlier draft used
        # "Join us in Berlin", which passes whether or not the match is
        # case-sensitive — the Berlin hint short-circuits first, so the
        # assertion could not fail. These strings carry no hint, no state
        # abbreviation and no spelled-out US marker, so the ONLY thing that
        # can return True is a case-insensitive bare-code match.
        assert positively_us_location("Join us") is False
        assert positively_us_location("Come work with us today") is False

    def test_us_inside_a_word_does_not_match(self) -> None:
        assert positively_us_location("Columbus") is False
        assert positively_us_location("Prussia") is False

    def test_a_foreign_hint_still_beats_the_bare_code(self) -> None:
        """Ordering guard: the non-US hint check runs first, so a string that
        names a foreign city cannot be rescued by a stray US token."""
        assert positively_us_location("Munich, DE - US HQ") is False


# ---------------------------------------------------------------------------
# #950 — the hint list split by consequence
# ---------------------------------------------------------------------------


class TestDisambiguationOnlyHints:
    """ "stuttgart" and "jerusalem" are needed to stop their countries' ISO
    codes reading as US states, but both are ALSO real US towns (Stuttgart,
    Arkansas; Jerusalem, New York/Ohio/Arkansas). While one list served both
    the ingest gate and the archive-veto disambiguation, the towns were
    silently rejected at ingest. #950 splits the lists: the ingest gate no
    longer knows these names; the disambiguation still does."""

    def test_stuttgart_arkansas_ingests(self) -> None:
        """The spelled-out form was the rejected one — "Stuttgart, AR" always
        survived because the state-abbreviation check short-circuits first."""
        assert is_us_location("Stuttgart, Arkansas") is True

    def test_jerusalem_new_york_ingests(self) -> None:
        assert is_us_location("Jerusalem, New York") is True

    def test_stuttgart_de_still_reads_as_germany_not_delaware(self) -> None:
        """The half the hint still buys: no archive veto for the German city
        with Delaware's state code."""
        assert positively_us_location("Stuttgart, DE") is False

    def test_jerusalem_il_still_reads_as_israel_not_illinois(self) -> None:
        assert positively_us_location("Jerusalem, IL") is False
