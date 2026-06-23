"""Anonymous contribution voting (#5 P3) against a live Supabase stack.

Proves the security-critical surface that the mock suite can't: votes are
RLS-scoped so a caller can only ever see/write their OWN vote (anonymity + no
spoofing), the service-role tally flips ``reference_jds.suppressed`` at the
net-downvote quorum, and a suppressed contribution drops out of the
shared-profile merge — restored if up-votes later rescue it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import pytest
from postgrest.exceptions import APIError
from supabase import Client

from app.models.targets import CategoryProfile, ScoringProfile
from app.services.targets import crud, votes
from app.services.targets.merge import merge_reference_jds

pytestmark = pytest.mark.integration


@pytest.fixture
def target_with_contribution(service_client: Client) -> Iterator[tuple[str, str]]:
    """A target + one reference-JD contribution. Yields (target_id, ref_jd_id)."""
    target_id: str = (
        service_client.table("targets")
        .insert({"label": f"P3 Vote {uuid.uuid4()}"})
        .execute()
        .data[0]["id"]
    )
    profile = ScoringProfile(
        categories={"core_skills": CategoryProfile(keywords={"React": 3}, weight=2.0)}
    )
    ref = crud.add_reference_jd(
        service_client,
        target_id=target_id,
        jd_text="reference jd body",
        jd_url=None,
        extracted_profile=profile,
        user_id=None,
    )
    try:
        yield target_id, ref.id
    finally:
        service_client.table("contribution_votes").delete().eq(
            "reference_jd_id", ref.id
        ).execute()
        service_client.table("reference_jds").delete().eq("target_id", target_id).execute()
        service_client.table("targets").delete().eq("id", target_id).execute()


def test_votes_are_anonymous_and_own_row_only(
    user_client_factory: Callable[[str], Client],
    two_seeded_users: tuple[str, str],
    target_with_contribution: tuple[str, str],
) -> None:
    uid_a, uid_b = two_seeded_users
    _, ref_id = target_with_contribution
    a = user_client_factory(uid_a)
    b = user_client_factory(uid_b)

    votes.set_user_vote(a, reference_jd_id=ref_id, user_id=uid_a, value=-1)
    votes.set_user_vote(b, reference_jd_id=ref_id, user_id=uid_b, value=-1)

    # A sees their own vote...
    assert votes.get_user_vote(a, reference_jd_id=ref_id, user_id=uid_a) == -1
    # ...but a raw RLS read returns ONLY A's row — B's vote is invisible, so no
    # one can tell who voted how (anonymity).
    visible = (
        a.table("contribution_votes")
        .select("user_id")
        .eq("reference_jd_id", ref_id)
        .execute()
        .data
    )
    assert [r["user_id"] for r in visible] == [uid_a]

    # And A cannot spoof a vote as B — RLS WITH CHECK rejects the insert.
    with pytest.raises(APIError):
        a.table("contribution_votes").insert(
            {"reference_jd_id": ref_id, "user_id": uid_b, "value": -1}
        ).execute()


def test_quorum_suppresses_and_upvote_rescues(
    service_client: Client,
    user_client_factory: Callable[[str], Client],
    two_seeded_users: tuple[str, str],
    target_with_contribution: tuple[str, str],
) -> None:
    uid_a, uid_b = two_seeded_users
    target_id, ref_id = target_with_contribution
    a = user_client_factory(uid_a)
    b = user_client_factory(uid_b)

    # One down-vote: net 1 < quorum 2 -> not suppressed.
    votes.set_user_vote(a, reference_jd_id=ref_id, user_id=uid_a, value=-1)
    assert votes.recompute_suppression(
        service_client, reference_jd_id=ref_id, quorum=2
    ) == (False, False)

    # Second down-vote: net 2 >= quorum 2 -> suppressed (and it CHANGED).
    votes.set_user_vote(b, reference_jd_id=ref_id, user_id=uid_b, value=-1)
    assert votes.recompute_suppression(
        service_client, reference_jd_id=ref_id, quorum=2
    ) == (True, True)
    # The merge drops the suppressed contribution — the only one here, so the
    # shared profile empties out.
    ref_jds = crud.list_reference_jds(service_client, target_id)
    assert all(j.suppressed for j in ref_jds)
    assert merge_reference_jds(ref_jds) == ScoringProfile()

    # A switches to an up-vote: net 0 < quorum -> rescued (un-suppressed).
    votes.set_user_vote(a, reference_jd_id=ref_id, user_id=uid_a, value=1)
    assert votes.recompute_suppression(
        service_client, reference_jd_id=ref_id, quorum=2
    ) == (False, True)
    ref_jds = crud.list_reference_jds(service_client, target_id)
    assert not any(j.suppressed for j in ref_jds)
    assert merge_reference_jds(ref_jds).categories["core_skills"].keywords == {"React": 3}
