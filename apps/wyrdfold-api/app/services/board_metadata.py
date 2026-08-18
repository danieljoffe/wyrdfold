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
"""

from __future__ import annotations

from typing import Any

from app.models.logistics import _COUNTRY_NAME_TO_ALPHA2
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


def board_columns(job: StandardJob) -> dict[str, Any]:
    """The ``jobs`` columns a board published for this posting.

    Mirrors :func:`app.services.extract.salary_columns` — one spread used by
    every write site so board facts land identically everywhere.

    Only keys the board actually supplied are returned. Callers spread this
    into an upsert payload; an absent key leaves the stored column untouched,
    which is what keeps a silent board from blanking a tagged value.

    NB ``country`` is deliberately NOT written here. ``jobs.country`` holds a
    DISPLAY vocabulary produced by ``location_parse`` (``US``, ``UK`` — not
    ``GB``), and #805 was exactly the bug where a filter sent alpha-2 against
    that column and matched nothing. Writing ISO codes into it would
    reintroduce that mismatch, so aligning the two vocabularies is its own
    deliberate change. ``StandardJob.country`` is still populated — the value
    is free and correct — it just has no safe column to land in yet.
    """
    cols: dict[str, Any] = {}
    if job.is_remote is not None:
        cols["is_remote"] = job.is_remote
    if job.employment_type:
        cols["employment_type"] = job.employment_type
    return cols
