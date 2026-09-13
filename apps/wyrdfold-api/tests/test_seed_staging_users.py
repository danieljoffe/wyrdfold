"""Guards for the staging persona seeder.

The two things that must never break: it cannot write to production, and the
personas it writes must be valid for the code that consumes them. The second
is easy to get wrong invisibly — a payload that inserts fine but fails
`OptimizedPayload.model_validate` produces accounts that exist and cannot be
scored, which looks like a scoring bug for as long as it takes to find.
"""

from __future__ import annotations

import pytest

from app.models.experience import OptimizedPayload
from scripts.seed_staging_users import (
    SeedError,
    assert_not_production,
    profile_row,
)
from scripts.staging_personas import PERSONAS
from scripts.sync_catalog_to_staging import PRODUCTION_REF, SyncError

STAGING_REF = "dyczsvaoqhvnafwwxuvf"
STAGING_URL = f"https://{STAGING_REF}.supabase.co"
PROD_URL = f"https://{PRODUCTION_REF}.supabase.co"


# --- the destination -------------------------------------------------------


def test_production_destination_is_refused() -> None:
    with pytest.raises(SeedError, match="PRODUCTION"):
        assert_not_production(PROD_URL, PRODUCTION_REF)


def test_staging_destination_is_allowed() -> None:
    """Guards the refusal above against being satisfiable by refusing all."""
    assert assert_not_production(STAGING_URL, STAGING_REF) == STAGING_REF


def test_confirmation_must_match_the_url() -> None:
    with pytest.raises(SeedError, match="Name the database"):
        assert_not_production(STAGING_URL, PRODUCTION_REF)


def test_confirmation_cannot_authorise_production() -> None:
    """Naming production correctly must NOT make it acceptable — the
    production check has to win over the confirmation check."""
    with pytest.raises(SeedError, match="PRODUCTION"):
        assert_not_production(PROD_URL, PRODUCTION_REF)


def test_unidentifiable_destination_is_refused() -> None:
    with pytest.raises(SeedError, match="could not identify"):
        assert_not_production("https://example.org", "whatever")


# --- the personas ----------------------------------------------------------


def test_every_payload_validates_against_the_real_model() -> None:
    """These payloads feed `derive_fit_score`, which takes an OptimizedPayload.
    Validating against the actual pydantic model (not a copy of its shape) is
    what makes the seeded accounts usable rather than merely present."""
    for persona in PERSONAS:
        parsed = OptimizedPayload.model_validate(persona["payload"])
        assert parsed.summary, f"{persona['email']} has no summary to score against"
        assert parsed.roles, f"{persona['email']} has no roles"
        assert parsed.skills, f"{persona['email']} has no skills"


def test_outcome_role_links_resolve() -> None:
    """`owner_role_id` returning None means the role<->outcome link is broken
    in both directions, which silently degrades what scoring can attribute."""
    for persona in PERSONAS:
        payload = OptimizedPayload.model_validate(persona["payload"])
        for outcome in payload.outcomes:
            assert payload.owner_role_id(outcome) is not None, (
                f"{persona['email']}: outcome {outcome.description!r} resolves to no role"
            )


def test_every_email_is_unroutable() -> None:
    """RFC 2606 reserves example.com precisely so test addresses cannot reach a
    real mailbox. Staging sends real magic-link mail, so a typo'd real domain
    here would email a stranger."""
    for persona in PERSONAS:
        assert persona["email"].endswith("@example.com"), persona["email"]


def test_emails_are_unique() -> None:
    """A duplicate would silently overwrite the earlier persona's profile."""
    emails = [p["email"] for p in PERSONAS]
    assert len(set(emails)) == len(emails)


def test_the_cast_actually_differs() -> None:
    """Personas that all look the same test nothing. Assert real spread across
    the axes the seeder exists to exercise."""
    plans = {p["plan"] for p in PERSONAS}
    assert plans == {"free", "trial", "starter", "pro"}, f"plans not covered: {plans}"
    assert any(not p["onboarded"] for p in PERSONAS), "nobody is mid-onboarding"
    assert any(not p.get("llm_enabled", True) for p in PERSONAS), "nobody has LLM off"
    assert any(p.get("unsubscribed") for p in PERSONAS), "nobody is unsubscribed"
    # An expired trial and a live one are different code paths (`trial_expired`).
    ages = {p.get("trial_age_days") for p in PERSONAS if p["plan"] == "trial"}
    assert any(a is not None and a > 30 for a in ages), "no EXPIRED trial"
    assert any(a is not None and a < 3 for a in ages), "no LIVE trial"


def test_skill_sets_are_distinct() -> None:
    """The whole point is different resume backgrounds. Identical skills would
    make five accounts score the same and prove nothing about matching."""
    sets = [frozenset(s["name"] for s in p["payload"]["skills"]) for p in PERSONAS]
    assert len(set(sets)) == len(sets), "two personas share an identical skill set"


# --- the profile row -------------------------------------------------------


def test_trial_clock_is_written_even_for_non_trial_plans() -> None:
    """Always-written, so an account later switched to 'trial' cannot inherit a
    stale clock from whatever the column happened to hold."""
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    for persona in PERSONAS:
        row = profile_row(persona, "00000000-0000-4000-8000-000000000000", now)
        assert "trial_started_at" in row
        if persona.get("trial_age_days") is None:
            assert row["trial_started_at"] is None


def test_unsubscribed_persona_has_notifications_off_both_ways() -> None:
    """`unsubscribed_at` and `job_notifications_enabled` must agree; a row that
    sets one and not the other is a state the app never produces."""
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    for persona in PERSONAS:
        row = profile_row(persona, "00000000-0000-4000-8000-000000000000", now)
        if persona.get("unsubscribed"):
            assert row["unsubscribed_at"] is not None
            assert row["job_notifications_enabled"] is False
        else:
            assert row["unsubscribed_at"] is None
            assert row["job_notifications_enabled"] is True


def test_mid_onboarding_persona_has_no_completion_stamp() -> None:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    unfinished = [p for p in PERSONAS if not p["onboarded"]]
    assert unfinished, "precondition: at least one persona is mid-onboarding"
    for persona in unfinished:
        row = profile_row(persona, "00000000-0000-4000-8000-000000000000", now)
        assert row["onboarding_completed_at"] is None
        assert row["onboarding_current_step"] is not None


def test_the_importers_exception_is_treated_as_a_refusal() -> None:
    """Reused helpers raise SyncError, not SeedError. If that stops being
    caught, a bad URL becomes a traceback and exit 1 — which reads as a broken
    seeder rather than a refused one."""
    from scripts.seed_staging_users import _REFUSALS

    assert SyncError in _REFUSALS
    assert SeedError in _REFUSALS


# --- idempotency -----------------------------------------------------------


def test_experience_write_declares_on_conflict(monkeypatch) -> None:
    """Re-running the seeder must not fail.

    `experience_optimized_docs` has a unique (user_id, version) constraint.
    Without on_conflict the SECOND run 409s on the first persona and abandons
    the other four — verified against staging before this test existed. Drives
    main() end to end so the assertion covers the URL actually requested, not a
    constant someone remembered to update.
    """
    import scripts.seed_staging_users as mod

    calls: list[tuple[str, str]] = []

    def fake_request(url, key, method, path, body=None):
        calls.append((method, path))
        if path.startswith("/auth/v1/admin/users") and method == "POST":
            return 201, {"id": "00000000-0000-4000-8000-000000000001"}
        return 201, []

    monkeypatch.setattr(mod, "_request", fake_request)
    monkeypatch.setenv("STAGING_SUPABASE_URL", STAGING_URL)
    monkeypatch.setenv("STAGING_SERVICE_KEY", "sb_secret_test")

    assert mod.main(["--confirm-write-to", STAGING_REF]) == 0

    experience = [p for _m, p in calls if p.startswith("/rest/v1/experience_optimized_docs")]
    assert experience, "the experience doc was never written"
    for path in experience:
        assert "on_conflict=user_id,version" in path, (
            f"experience write without on_conflict: {path} — a re-run will 409"
        )


def test_profile_write_declares_on_conflict(monkeypatch) -> None:
    """Same hazard, same shape: a second run must update, not duplicate."""
    import scripts.seed_staging_users as mod

    calls: list[str] = []

    def fake_request(url, key, method, path, body=None):
        calls.append(path)
        if path.startswith("/auth/v1/admin/users") and method == "POST":
            return 201, {"id": "00000000-0000-4000-8000-000000000001"}
        return 201, []

    monkeypatch.setattr(mod, "_request", fake_request)
    monkeypatch.setenv("STAGING_SUPABASE_URL", STAGING_URL)
    monkeypatch.setenv("STAGING_SERVICE_KEY", "sb_secret_test")
    assert mod.main(["--confirm-write-to", STAGING_REF]) == 0

    profiles = [p for p in calls if p.startswith("/rest/v1/user_profiles")]
    assert profiles, "no profile was written"
    assert all("on_conflict=user_id" in p for p in profiles)


def test_the_invite_is_written_before_the_account(monkeypatch) -> None:
    """Order matters on a partial failure. Invite-then-account leaves a
    recoverable state (invited, no account — just sign in). The reverse leaves
    an account the auth hook refuses to admit."""
    import scripts.seed_staging_users as mod

    calls: list[str] = []

    def fake_request(url, key, method, path, body=None):
        calls.append(path)
        if path.startswith("/auth/v1/admin/users") and method == "POST":
            return 201, {"id": "00000000-0000-4000-8000-000000000001"}
        return 201, []

    monkeypatch.setattr(mod, "_request", fake_request)
    monkeypatch.setenv("STAGING_SUPABASE_URL", STAGING_URL)
    monkeypatch.setenv("STAGING_SERVICE_KEY", "sb_secret_test")
    assert mod.main(["--confirm-write-to", STAGING_REF]) == 0

    first_invite = next(i for i, p in enumerate(calls) if "wyrdfold_beta_invites" in p)
    first_account = next(i for i, p in enumerate(calls) if "admin/users" in p)
    assert first_invite < first_account, "an account was created before its invite"


def test_dry_run_writes_nothing(monkeypatch) -> None:
    import scripts.seed_staging_users as mod

    calls: list[str] = []
    monkeypatch.setattr(
        mod,
        "_request",
        lambda *a, **k: (calls.append(a[3]), (201, []))[1],
    )
    monkeypatch.setenv("STAGING_SUPABASE_URL", STAGING_URL)
    monkeypatch.setenv("STAGING_SERVICE_KEY", "sb_secret_test")
    assert mod.main(["--confirm-write-to", STAGING_REF, "--dry-run"]) == 0
    assert calls == [], f"--dry-run issued writes: {calls}"


def test_production_refusal_reaches_no_write(monkeypatch) -> None:
    """The refusal must happen before the first request, not after."""
    import scripts.seed_staging_users as mod

    calls: list[str] = []
    monkeypatch.setattr(
        mod,
        "_request",
        lambda *a, **k: (calls.append(a[3]), (201, []))[1],
    )
    monkeypatch.setenv("STAGING_SUPABASE_URL", PROD_URL)
    monkeypatch.setenv("STAGING_SERVICE_KEY", "sb_secret_test")
    assert mod.main(["--confirm-write-to", PRODUCTION_REF]) == 2
    assert calls == []


# --- the invite write is the lock, so it must fail closed -----------------


def _fake_transport(monkeypatch, *, invite_status: int = 201):
    """Record every request; let the caller break the invite write."""
    import scripts.seed_staging_users as mod

    calls: list[tuple[str, str]] = []

    def fake_request(url, key, method, path, body=None):
        calls.append((method, path))
        if "wyrdfold_beta_invites" in path:
            return invite_status, ([] if invite_status < 300 else {"message": "nope"})
        if path.startswith("/auth/v1/admin/generate_link"):
            return 200, {"action_link": "https://example.invalid/verify?token=x"}
        if path.startswith("/auth/v1/admin/users") and method == "POST":
            return 201, {"id": "00000000-0000-4000-8000-000000000001"}
        return 201, []

    monkeypatch.setattr(mod, "_request", fake_request)
    monkeypatch.setenv("STAGING_SUPABASE_URL", STAGING_URL)
    monkeypatch.setenv("STAGING_SERVICE_KEY", "sb_secret_test")
    return mod, calls


def test_a_failed_invite_creates_no_account(monkeypatch) -> None:
    """The ordering comment claims a partial failure leaves a recoverable
    state. That is only true if a failed invite STOPS us — otherwise we create
    an account the auth hook will refuse, which is the exact state the ordering
    was supposed to prevent, reported as success."""
    mod, calls = _fake_transport(monkeypatch, invite_status=500)
    with pytest.raises(SeedError, match="invite write failed"):
        mod.main(["--confirm-write-to", STAGING_REF])
    assert not any("admin/users" in p for _m, p in calls), (
        "an auth user was created after the invite failed"
    )


@pytest.mark.parametrize("status", [400, 401, 403, 409, 500, 503])
def test_every_failing_invite_status_stops_the_run(monkeypatch, status) -> None:
    """409 included: with merge-duplicates an existing invite returns 200, so a
    409 means the header is gone and this is a real conflict, not a re-run."""
    mod, calls = _fake_transport(monkeypatch, invite_status=status)
    with pytest.raises(SeedError):
        mod.main(["--confirm-write-to", STAGING_REF])
    assert not any("admin/users" in p for _m, p in calls)


@pytest.mark.parametrize("status", [200, 201])
def test_both_success_statuses_are_accepted(monkeypatch, status) -> None:
    """Measured against the real API: merge-duplicates returns 200 for an
    existing email and 201 for a new one. Accepting only 201 would make every
    re-run fail; accepting anything would make the check useless."""
    mod, calls = _fake_transport(monkeypatch, invite_status=status)
    assert mod.main(["--confirm-write-to", STAGING_REF]) == 0
    assert any("admin/users" in p for _m, p in calls)


def test_invite_write_still_carries_the_merge_header() -> None:
    """Without `resolution=merge-duplicates` an existing invite returns 409 —
    verified against staging. The header is what makes 200 possible, so the
    status check above and this header have to travel together."""
    import inspect

    import scripts.seed_staging_users as mod

    assert "resolution=merge-duplicates" in inspect.getsource(mod._request)


# --- getting in ------------------------------------------------------------


def test_login_links_are_not_printed_by_default(monkeypatch, capsys) -> None:
    """They are live credentials for a staging account. Opt-in, not ambient."""
    mod, calls = _fake_transport(monkeypatch)
    assert mod.main(["--confirm-write-to", STAGING_REF]) == 0
    assert not any("generate_link" in p for _m, p in calls)
    out = capsys.readouterr().out
    assert "--login-links" in out, "the run should say how to actually sign in"


def test_login_links_uses_the_admin_endpoint_that_sends_no_mail(monkeypatch) -> None:
    """`admin/generate_link` RETURNS the link. Using the ordinary OTP endpoint
    would mail an unroutable address and hand back nothing."""
    mod, calls = _fake_transport(monkeypatch)
    assert mod.main(["--confirm-write-to", STAGING_REF, "--login-links"]) == 0
    generated = [p for _m, p in calls if "generate_link" in p]
    assert len(generated) == len(PERSONAS), "not every persona got a link"
    assert not any(p == "/auth/v1/otp" for _m, p in calls), "this would send mail"


def test_a_failed_link_is_a_refusal_not_a_blank(monkeypatch) -> None:
    """Printing an empty line where a credential should be is worse than
    failing: it reads as 'no link needed'."""
    import scripts.seed_staging_users as mod

    monkeypatch.setattr(mod, "_request", lambda *a, **k: (500, {"msg": "down"}))
    with pytest.raises(SeedError, match="could not generate a login link"):
        mod.generate_login_link(STAGING_URL, "k", "priya.raghavan@example.com")


def test_dry_run_generates_no_links(monkeypatch) -> None:
    """--dry-run must not mint credentials as a side effect."""
    mod, calls = _fake_transport(monkeypatch)
    assert mod.main(["--confirm-write-to", STAGING_REF, "--dry-run", "--login-links"]) == 0
    assert calls == []
