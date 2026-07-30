"""The family-gate predicate (#277/#278) — the single Python definition.

Strict, keep-null: a (target, job) pair passes only when the families match,
the job is untagged (``role_family`` NULL — benefit of the doubt, same spirit
as ``is_us IS NOT FALSE``), or the target is unclassified (NULL → ungated).

Every Python consumer of "is this job's family compatible with this target"
MUST route through this function rather than re-deriving the comparison —
the target-membership badge shipped a third private copy of the rule (in
effect: no rule at all) and off-family listings badged as "In '<target>'"
on /search. Same drift class as the #531 search-filter params.

The SQL twin lives in ``get_target_jobs`` / ``get_cross_target_jobs``
(``supabase/migrations/20260729100000_jobs_salary_parts.sql``):

    AND (v_family IS NULL OR jp.role_family IS NULL OR jp.role_family = v_family)

Change both or neither.
"""

from __future__ import annotations


def passes_family_gate(target_family: str | None, job_family: str | None) -> bool:
    """True when ``job_family`` is admissible for ``target_family``."""
    return target_family is None or job_family is None or job_family == target_family
