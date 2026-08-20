"""Python/SQL parity for the family gate (migration 20260815200000).

The identical rule runs in two places: ``passes_family_gate`` in Python (the
poller, the membership badge) and ``public.family_gate_passes`` in Postgres
(behind both /jobs RPCs). Drift between them is a failure this repo has already
shipped twice — the badge once carried a third private copy of the rule and
off-family listings got badged as "In \'<target>\'" on /search, and the #531
search-filter params drifted the same way. So every combination is compared
against the live database, not a mock.
"""

from __future__ import annotations

import itertools

import pytest
from supabase import Client

from app.services.qualification.family_gate import passes_family_gate
from app.services.qualification.tagger import RoleFamily

pytestmark = pytest.mark.integration

_FAMILIES: tuple[str, ...] = tuple(RoleFamily.__args__)  # type: ignore[attr-defined]


def test_sql_matches_python_case_for_case(service_client: Client) -> None:
    """Every (target, job) pair must agree between Python and Postgres."""
    values = [*_FAMILIES, None]
    mismatches: list[tuple[str | None, str | None, bool, object]] = []
    for target, job in itertools.product(values, values):
        expected = passes_family_gate(target, job)
        got = service_client.rpc(
            "family_gate_passes",
            {"p_target_family": target, "p_job_family": job},
        ).execute()
        if bool(got.data) is not expected:
            mismatches.append((target, job, expected, got.data))
    assert not mismatches, f"Python/SQL family-gate disagreement: {mismatches}"
