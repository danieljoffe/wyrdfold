"""The family-gate predicate (#277/#278) — the single Python definition.

Keep-null, unknown-tolerant, adjacency-aware. A (target, job) pair passes when:

* the target is unclassified (``role_family`` NULL → ungated); or
* the job's family is UNKNOWN — NULL (untagged) or ``"other"`` (the tagger's
  catch-all) — benefit of the doubt, same spirit as ``is_us IS NOT FALSE``; or
* the families match exactly; or
* the families are ADJACENT (see ``_ADJACENT``).

Every Python consumer of "is this job's family compatible with this target"
MUST route through this function rather than re-deriving the comparison —
the target-membership badge shipped a third private copy of the rule (in
effect: no rule at all) and off-family listings badged as "In '<target>'"
on /search. Same drift class as the #531 search-filter params.

WHY ``"other"`` COUNTS AS UNKNOWN (2026-08-15). ``other`` is what the tagger
emits when it cannot classify a posting — and also what
``_coerce_literal`` substitutes when the model returns a malformed
``role_family``. So it means "don't know", exactly like NULL. The gate used
to treat the two opposite ways: a missing answer passed, an explicit "I
can't tell" was excluded. That silently hid **1,852 live postings — 11.1% of
the catalog** from every classified target. Consistency, not a loosening:
this only affects jobs whose family is unknown, never jobs known to be in a
different discipline.

ADJACENCY IS DELIBERATELY NOT IMPLEMENTED. An earlier plan paired
``engineering``<->``data_ml`` and ``sales``<->``customer_experience`` to blunt
misclassification, because folding skill extraction into the tagger's prompt
degraded ``role_family`` (95.0% -> 90.7% over 10 golden runs) and an
exact-match gate turns every such error into a silently hidden job. That
extraction now runs as a zero-cost dictionary (``skill_dictionary.py``)
instead, so the tagger prompt is untouched, the regression never happens, and
the mitigation is unnecessary. Widening what every target matches is a product
decision about how tight matching should feel — it should be made on its own
merits, with its own evidence, not smuggled in as a side effect of a cost fix.

The SQL twin lives in ``public.family_gate_passes``
(``supabase/migrations/20260815200000_family_gate_unknown_tolerant.sql``),
which both ``/jobs`` RPCs call. Change both or neither —
``tests/integration/test_family_gate_parity.py`` pins them case-for-case.
"""

from __future__ import annotations

# Values that mean "we could not classify this posting". Treated as a pass
# (benefit of the doubt) rather than an exclusion.
_UNKNOWN_JOB_FAMILIES: frozenset[str] = frozenset({"other"})


def passes_family_gate(target_family: str | None, job_family: str | None) -> bool:
    """True when ``job_family`` is admissible for ``target_family``."""
    if target_family is None:
        return True  # unclassified target — ungated
    if job_family is None or job_family in _UNKNOWN_JOB_FAMILIES:
        return True  # unknown job family — benefit of the doubt
    return job_family == target_family
