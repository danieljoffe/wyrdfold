"""Normalize board-published job metadata into our column vocabularies (#846).

Ashby, Lever and SmartRecruiters publish remote status, country, employment
type and department as STRUCTURED fields — facts the employer stated, not
inferences. We were paying an LLM to guess at them anyway: the qualification
tagger writes ``jobs.is_remote`` / ``employment_type``, and Phase 2 writes
``scores.logistics_filters.remote_status`` / ``country``. Two inference paths
over the same question also disagree — #795 measured 229 prod contradictions
on remote status alone.

Each provider spells the same fact differently, so the fetchers hand us raw
values and this module maps them onto the column vocabularies:

    is_remote        bool                      → jobs.is_remote
    employment_type  qualification vocabulary   → jobs.employment_type
    country          ISO 3166-1 alpha-2, upper  → jobs.country (DISPLAY form,
                                                  see :func:`display_country`)
                                                  and :func:`board_us_verdict`

DESIGN RULE — silence is not falsity. Every normalizer returns ``None`` for
anything it doesn't positively recognize, and :func:`board_columns` omits
``None`` keys entirely. A board that doesn't publish a field must never blank
a value another path already established, and an unrecognized string must
never be coerced into a plausible-looking wrong answer.

The omission is only half the rule — the WRITE has to honour it too. A
PostgREST *bulk* upsert builds one statement from the union of the keys across
the whole payload, so a key one row supplies is written to every row, and the
rows that omitted it get ``NULL`` (#928). Bulk-write these columns through
:func:`app.services.db_write.poll_db_upsert`, which partitions the batch by
key-set so each statement is homogeneous. Spreading them straight into a bulk
``.upsert(list)`` reintroduces the blanking.
"""

from __future__ import annotations

from typing import Any

from app.models.logistics import _COUNTRY_NAME_TO_ALPHA2
from app.services.location_parse import (
    _COUNTRIES,
    _US_STATE_ABBREVS,
    LocationParts,
    parse_location,
)
from app.services.qualification.heuristics import positively_us_location
from app.services.standard_job import StandardJob

# Provider spellings → ``jobs.employment_type`` (tagger.EmploymentType).
# Keys are casefolded and stripped of separators, so "Full-Time", "FullTime"
# and "full time" all collapse to one entry.
_EMPLOYMENT_TYPE: dict[str, str] = {
    "fulltime": "full_time",
    "permanent": "full_time",  # smartrecruiters typeOfEmployment.id
    "regular": "full_time",
    "parttime": "part_time",
    "contract": "contract",
    "contractor": "contract",
    "temporary": "temporary",
    "temp": "temporary",
    "seasonal": "temporary",
    "intern": "internship",
    "internship": "internship",
}

# Provider spellings of a workplace/remote descriptor → is_remote.
# Hybrid is deliberately NOT remote: the /jobs remote filter means
# "can I work remotely", and a hybrid role requires office presence.
_WORKPLACE_REMOTE: dict[str, bool] = {
    "remote": True,
    "fullyremote": True,
    "onsite": False,
    "inoffice": False,
    "hybrid": False,
}


def _key(value: Any) -> str | None:
    """Casefold and strip separators so provider spellings collapse."""
    if not isinstance(value, str):
        return None
    k = "".join(ch for ch in value if ch.isalnum()).casefold()
    return k or None


def normalize_employment_type(raw: Any) -> str | None:
    """Provider employment/commitment string → ``jobs.employment_type``."""
    key = _key(raw)
    return _EMPLOYMENT_TYPE.get(key) if key else None


# The real ISO 3166-1 alpha-2 register. A bare two-letter string is only a
# country if it is actually ASSIGNED — "accept anything two letters long" reads
# a US state code as a country, and since this value now drives a ONE-WAY
# archive, "TX" / "NY" / "FL" / "OH" would each prune a US role. Roughly half of
# the USPS abbreviations disappear here; the rest are handled by
# ``_STATE_AMBIGUOUS_ALPHA2`` below, because they are genuinely both.
# The register itself, whitespace-delimited and grouped by initial letter. A 249
# element list literal is what SIM905 wants; it is 249 lines of noise for a static
# reference table nobody reads element-by-element, so the split stays.
_ISO_3166_1_ALPHA2: frozenset[str] = frozenset(
    """
    AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ
    BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ
    CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ
    DE DJ DK DM DO DZ
    EC EE EG EH ER ES ET
    FI FJ FK FM FO FR
    GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY
    HK HM HN HR HT HU
    ID IE IL IM IN IO IQ IR IS IT
    JE JM JO JP
    KE KG KH KI KM KN KP KR KW KY KZ
    LA LB LC LI LK LR LS LT LU LV LY
    MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ
    NA NC NE NF NG NI NL NO NP NR NU NZ
    OM
    PA PE PF PG PH PK PL PM PN PR PS PT PW PY
    QA
    RE RO RS RU RW
    SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ
    TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ
    UA UG UM US UY UZ
    VA VC VE VG VI VN VU
    WF WS
    YE YT
    ZA ZM ZW
    """.split()  # noqa: SIM905
)

# US territories with their own ISO code. They ARE the United States for every
# purpose this module serves, and a live Lever posting located "American Samoa"
# was being archived as foreign before this set existed. ``VG`` is deliberately
# absent — the BRITISH Virgin Islands is not the US.
_US_TERRITORY_ALPHA2: frozenset[str] = frozenset({"PR", "GU", "VI", "AS", "MP", "UM"})

# ISO codes that are ALSO USPS state abbreviations, derived from
# ``location_parse``'s own state table so the two cannot drift. A board that put
# a state code in its country field would look exactly like one of these, and
# the archive is one-way — so a code in this set needs the location string to
# not read as US before it may prune. Measured across 21,891 live postings
# carrying a board country: 2,943 use one of these codes and not one was a US
# state (CA→"Ontario - Remote", DE→"Stuttgart", MD→"Chișinău", MT→"Valetta",
# PA→"Panama City, Panama"), so the guard costs almost nothing and covers the
# case we cannot otherwise see coming.
_STATE_AMBIGUOUS_ALPHA2: frozenset[str] = frozenset(
    (_ISO_3166_1_ALPHA2 & _US_STATE_ABBREVS) - _US_TERRITORY_ALPHA2
)


def normalize_country(raw: Any) -> str | None:
    """Provider country value → ISO 3166-1 alpha-2, upper-case.

    Accepts both a code (Lever's ``AR``, SmartRecruiters' ``de``) and a name
    (Ashby's ``United States``), reusing the map the Phase-2 grader validator
    already carries so the two paths can't drift apart.

    A bare code must be an ASSIGNED ISO 3166-1 alpha-2 value. Two letters and
    alphabetic is not enough: ``location_parse`` refuses bare codes outright
    ("too collision-prone"), and while a board's structured country field earns
    more trust than a free-text location, it does not earn a blank cheque —
    ``TX`` is not a country in any register.
    """
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    mapped = _COUNTRY_NAME_TO_ALPHA2.get(stripped.casefold())
    if mapped:
        return mapped.upper()
    # A bare alpha-2 code — only if it is actually an assigned one.
    if len(stripped) == 2 and stripped.isalpha():
        code = stripped.upper()
        return code if code in _ISO_3166_1_ALPHA2 else None
    return None  # unrecognized — say nothing rather than guess


# ISO 3166-1 alpha-2 → the DISPLAY spelling ``location_parse`` produces, built
# by COMPOSING that module's own token table with the grader's name→alpha-2 map
# rather than hand-listing pairs. Neither vocabulary can drift out from under
# the other: a country only appears here when both modules already know it, and
# the value written to ``jobs.country`` is byte-identical to what the parser
# would have produced from the location string.
#
# ``location_parse`` deliberately omits the bare tokens "ca" / "il" / "in"
# because in a free-text LOCATION they read as California / Illinois / Indiana.
# That ambiguity does not exist here: this map is keyed on a board's STRUCTURED
# ISO country field, where ``CA`` can only be Canada. Composing through the
# country NAME is what lets Canada/India back in without reopening the
# location-string ambiguity.
_ISO_TO_DISPLAY_COUNTRY: dict[str, str] = {
    alpha2: _COUNTRIES[name]
    for name, alpha2 in _COUNTRY_NAME_TO_ALPHA2.items()
    if name in _COUNTRIES
}


def display_country(alpha2: str | None) -> str | None:
    """ISO 3166-1 alpha-2 → the ``jobs.country`` DISPLAY spelling, or ``None``.

    ``jobs.country`` holds a display vocabulary (``US``, ``UK`` — never ``GB``),
    and #805 was exactly the bug where a filter sent alpha-2 against it and
    matched nothing (GB 0→2,619 rows once fixed). So the board's code is
    TRANSLATED on the way in rather than stored raw, and a code with no display
    spelling (``CH``, ``ZA``) writes nothing at all — the column never gains a
    value ``location_parse`` could not have produced. The verdict in
    :func:`board_us_verdict` does not go through this map: not being able to
    SPELL a country has no bearing on whether it is the United States.
    """
    if not alpha2:
        return None
    return _ISO_TO_DISPLAY_COUNTRY.get(alpha2.upper())


def board_us_verdict(job: StandardJob) -> bool | None:
    """Is this posting US-based, according to the country the BOARD published?

    ``None`` means the board said nothing recognizable — the caller must leave
    ``jobs.is_us`` untouched, because silence is not falsity and the lazy
    qualification tagger may fill it in later.

    Why this exists: qualification tagging went lazy (2026-08-26), so nothing
    stamps ``is_us`` at ingest any more and a fresh listing stays ``NULL`` —
    which ``is_us IS NOT FALSE`` admits — until something grades it. The
    poller's L1 ``is_us_location`` gate cannot close that hole: it drops a
    listing only when the location string carries a known non-US hint AND no US
    marker, so by construction every row it ADMITS is one the same parser
    cannot call non-US. The board's structured country is a different, stronger
    fact — the employer's own answer — and it is free.

    ASYMMETRIC on purpose, mirroring ``board_columns``' remote rule. A US
    verdict is recorded as-is. A NON-US verdict is withheld when
    ``positively_us_location`` says the location string plainly names the US:
    that is the #60 workstream-B veto, and it covers the multi-country posting
    whose postal address is abroad but which also lists a US office
    ("New York, London"). Withholding leaves ``NULL``, so the LLM still gets to
    judge it later — the conservative direction.

    US TERRITORIES are the United States. ``PR`` / ``GU`` / ``VI`` / ``AS`` /
    ``MP`` / ``UM`` carry their own ISO codes and would otherwise read as
    foreign; a live Lever posting located "American Samoa" was doing exactly
    that.

    STATE-CODE AMBIGUITY. ``CA`` is Canada in ISO and California in a US
    address, and this verdict drives a ONE-WAY archive that nothing ever
    reverses. So for a code in ``_STATE_AMBIGUOUS_ALPHA2`` the bar is raised:
    the location's own deterministic parse must not say US either. That is
    strictly more than ``positively_us_location`` asks (which needs an explicit
    marker or a "City, ST" form) — it also catches the bare-metro shapes,
    "San Diego" or "Austin". Cost measured on live boards: of 883 admitted
    postings carrying an ambiguous code, this withholds exactly one
    ("Sydney, CA"), and Lever's geocoded field probably means Nova Scotia
    there anyway.
    """
    code = job.country
    if code is None:
        return None
    if code == "US" or code in _US_TERRITORY_ALPHA2:
        return True
    if positively_us_location(job.location_name):
        return None
    if code in _STATE_AMBIGUOUS_ALPHA2 and parse_location(job.location_name).country == "US":
        return None
    return False


def normalize_remote(
    *, is_remote: Any = None, workplace_type: Any = None, is_hybrid: Any = None
) -> bool | None:
    """Resolve a provider's remote signals into ``jobs.is_remote``.

    Providers give this in three shapes and sometimes several at once:
    a boolean (Ashby ``isRemote``, SmartRecruiters ``location.remote``), a
    descriptor (Ashby ``workplaceType``, Lever ``workplaceType``), and a
    separate hybrid boolean (SmartRecruiters ``location.hybrid``).

    An explicit hybrid flag wins over a bare ``remote=False`` because it is
    strictly more specific; otherwise the boolean wins over the descriptor,
    since it is the field the provider models rather than labels.
    """
    if is_hybrid is True:
        return False
    if isinstance(is_remote, bool):
        return is_remote
    key = _key(workplace_type)
    return _WORKPLACE_REMOTE.get(key) if key else None


def board_columns(job: StandardJob, location: LocationParts | None = None) -> dict[str, Any]:
    """The ``jobs`` columns a board published for this posting.

    Mirrors :func:`app.services.extract.salary_columns` — one spread used by
    every write site so board facts land identically everywhere.

    Only keys the board actually supplied are returned, which is what keeps a
    silent board from blanking a tagged value — PROVIDED the write honours the
    omission. A single-row upsert does. A BULK upsert does not: PostgREST
    builds one statement from the union of the batch's keys and sends ``NULL``
    for the rows that omitted one, so a board-speaking posting blanks its
    board-silent neighbour (#928, the same contradiction #795 measured and #851
    fixed on the tagger side; measured against the local stack, not assumed).
    Bulk callers must route through
    :func:`app.services.db_write.poll_db_upsert`, which partitions by key-set
    so every statement is homogeneous and the omission holds exactly.

    ``is_us`` is still NOT returned here, even though the board states it. It
    is conditional per row — the verdict is withheld for an ambiguous location
    — so it would be the most exposed key of all if the partitioning ever
    regressed, and the value drives an IRREVERSIBLE archive. The poller writes
    it with a targeted post-upsert UPDATE instead: see
    ``poller._apply_board_us_verdicts`` and :func:`board_us_verdict`. That
    UPDATE predates ``poll_db_upsert`` and was written to dodge #928; it is now
    belt-and-braces rather than the only protection, and worth keeping for a
    write whose mistakes cannot be undone.

    ``country`` IS returned, and is safe for the same reason the rule above
    makes ``is_us`` unsafe: every poller payload already carries ``country``
    unconditionally (from ``location_parse``), so this key is uniform across
    the batch and merely overrides a value that was going to be written anyway.
    It is written in the DISPLAY vocabulary via :func:`display_country`, never
    as a raw ISO code — writing ``GB`` into a column the filters read as ``UK``
    is #805 verbatim. The board wins over the parse where the two disagree AND
    the verdict below is decidable, which is rare and one-sided: across 23,916
    live postings on 35 real boards the two agreed 15,439 times and disagreed
    22, the board was right in every sampled disagreement ("CA - Toronto"
    parses as California, "IN - Bangalore" as Indiana, "London, ON" as the UK),
    and 8 overrides survive the veto below. It also fills 3,730 postings the
    parser could not read a country from at all.

    ONE PREDICATE FOR BOTH WRITES: ``country`` is written only when
    :func:`board_us_verdict` reaches a verdict at all. Any reason to distrust
    the board's country enough to withhold a US/non-US call — a US marker in
    the location string, an unresolvable state-code collision — is equally a
    reason not to FILE the posting under that country. Letting the two sites
    disagree would put a "New York, NY; London" role under ``UK`` in the /jobs
    filter while refusing to prune it, and would disable the ``country = 'US'``
    veto in ``qualification.materialize`` on exactly the multi-country class it
    was written for.

    ``location`` is the deterministic parse of the board's location string
    (``location_parse.parse_location``). Greenhouse and Workday publish no
    remote flag at all, but they routinely say it in the location itself
    ("Remote - US", "Remote; New York, NY"), and that parse is already
    board-stated ground truth rather than an inference. It is used ONLY as a
    fallback: a provider that models the field outright always wins.

    ASYMMETRY, deliberately: a location saying "Remote" proves remote, but a
    location NOT saying it proves nothing — plenty of remote roles just list
    an office. So ``location.remote`` is promoted only when True. Treating its
    False as "on-site" would assert a fact we don't have about most of the
    corpus, which is the same mistake as letting a silent board write False.
    """
    cols: dict[str, Any] = {}
    if location is not None and location.remote:
        cols["is_remote"] = True
    if job.is_remote is not None:
        cols["is_remote"] = job.is_remote  # a board that states it always wins
    if job.employment_type:
        cols["employment_type"] = job.employment_type
    if board_us_verdict(job) is not None:
        display = display_country(job.country)
        if display:
            cols["country"] = display
    return cols
