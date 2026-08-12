"""Logistics filters extracted by the Phase 2 grader.

Groundwork for plan-wyrdfold-logistics-chips.md. The Phase 2 prompt
change that actually emits these fields lands in a follow-up PR; this
module is the schema half — defines the shape and validates anything
that does write to the column today (e.g. backfill scripts).

Filter-only: the values inform the /jobs chips and filter query
params (?remote_only=true, ?min_salary=150000, ?country=US). They
never affect ``score``, ``recency_score``, or sort order.

See the "Concepts" block in plan-wyrdfold-streamlined-target.md for
the distinction between **axis weights** (score-tuning, lives on
user_targets) and **logistics filters** (list-filtering, lives on
scores). They are independent mechanisms and never collide in the
data path.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

RemoteStatus = Literal["remote", "hybrid", "onsite", "unspecified"]
SalaryUnit = Literal["year", "hour"]

# Country NAME → ISO 3166-1 alpha-2, for the occasional grader response that
# writes the name instead of the code.
#
# The column's vocabulary is alpha-2 and the grader nearly always honours it —
# all 2,027 populated prod rows are codes (AE, AL, …, ZA). But "nearly" is not
# a guarantee: on 2026-08-11 one response returned "India", and because
# ``complete_json`` validates the WHOLE payload in one shot, a single 5-char
# string took out the entire fit-score call (#693). Normalizing here keeps the
# tight column contract while making that class of slip survivable.
#
# Keys are the country forms that actually occur in ``jobs.country`` (the
# deterministic location parser's display vocabulary), so the two conventions
# meet here rather than drifting further apart. Anything NOT in this map still
# fails ``max_length`` — a genuinely unrecognized string is a real problem and
# should stay loud.
_COUNTRY_NAME_TO_ALPHA2: dict[str, str] = {
    "australia": "AU",
    "brazil": "BR",
    "canada": "CA",
    "colombia": "CO",
    "costa rica": "CR",
    "france": "FR",
    "germany": "DE",
    "hungary": "HU",
    "india": "IN",
    "ireland": "IE",
    "italy": "IT",
    "japan": "JP",
    "malaysia": "MY",
    "mexico": "MX",
    "netherlands": "NL",
    "new zealand": "NZ",
    "philippines": "PH",
    "poland": "PL",
    "portugal": "PT",
    "singapore": "SG",
    "spain": "ES",
    "ukraine": "UA",
    # Long forms of the two the parser abbreviates by display convention.
    "united states": "US",
    "united states of america": "US",
    "united kingdom": "GB",
    "great britain": "GB",
}


class LogisticsFilters(BaseModel):
    """Structured logistics observed in the JD by the Phase 2 grader.

    Every field is optional / has a sentinel ``unspecified`` value;
    the grader is instructed to lean conservative and say "I don't
    know" rather than guess. False positives on filter pills are worse
    than misses — a "Remote only" filter that surfaces hybrid roles
    looks broken; a slightly under-populated chip list does not.

    Salary fields are intended to capture the explicit disclosed range
    only. "Competitive salary" / "DOE" / equity-only postings stay
    NULL across all four salary fields.

    Location fields capture the primary office anchor when one is
    named. A remote-only role with no anchor city / country leaves
    both NULL — the ``remote_status`` field carries the signal in
    that case.
    """

    remote_status: RemoteStatus = "unspecified"

    salary_min: int | None = Field(default=None, ge=0)
    salary_max: int | None = Field(default=None, ge=0)
    salary_currency: str | None = Field(default=None, max_length=8)
    salary_unit: SalaryUnit | None = None

    location_city: str | None = Field(default=None, max_length=120)
    location_country: str | None = Field(default=None, max_length=4)

    @field_validator("location_country", mode="before")
    @classmethod
    def _normalize_country(cls, value: object) -> object:
        """Map a recognized country NAME onto its alpha-2 code (#693).

        Runs BEFORE ``max_length``, so "India" becomes "IN" and validates
        instead of failing the whole grader response. Values already in the
        column's vocabulary pass through untouched; anything unrecognized is
        left alone and still trips ``max_length`` — normalizing is for known
        countries, not a licence to accept free text.
        """
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        return _COUNTRY_NAME_TO_ALPHA2.get(stripped.casefold(), stripped)

    def has_any_signal(self) -> bool:
        """Whether this row carries any non-default information.

        Useful for the FE chip renderer: when this returns False the
        chip row can be skipped entirely rather than rendering an
        empty container.
        """
        return (
            self.remote_status != "unspecified"
            or self.salary_min is not None
            or self.salary_max is not None
            or self.location_city is not None
            or self.location_country is not None
        )
