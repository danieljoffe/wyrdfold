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
    country          ISO 3166-1 alpha-2, upper  → NO COLUMN YET, see
                                                  :func:`board_columns`

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
from app.services.location_parse import LocationParts
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


def normalize_country(raw: Any) -> str | None:
    """Provider country value → ISO 3166-1 alpha-2, upper-case.

    Accepts both a code (Lever's ``AR``, SmartRecruiters' ``de``) and a name
    (Ashby's ``United States``), reusing the map the Phase-2 grader validator
    already carries so the two paths can't drift apart.
    """
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    mapped = _COUNTRY_NAME_TO_ALPHA2.get(stripped.casefold())
    if mapped:
        return mapped.upper()
    # Already a bare alpha-2 code.
    if len(stripped) == 2 and stripped.isalpha():
        return stripped.upper()
    return None  # unrecognized — say nothing rather than guess


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
    fixed on the tagger side). Bulk callers must route through
    :func:`app.services.db_write.poll_db_upsert`, which partitions by key-set.

    NB ``country`` is deliberately NOT written here. ``jobs.country`` holds a
    DISPLAY vocabulary produced by ``location_parse`` (``US``, ``UK`` — not
    ``GB``), and #805 was exactly the bug where a filter sent alpha-2 against
    that column and matched nothing. Writing ISO codes into it would
    reintroduce that mismatch, so aligning the two vocabularies is its own
    deliberate change. ``StandardJob.country`` is still populated — the value
    is free and correct — it just has no safe column to land in yet.

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
    return cols
