from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import notify


class _ExecuteStub:
    """Captures the terminal `.execute()` result for a mocked Supabase chain."""

    def __init__(self, data: list[dict] | None, count: int | None = None):
        self.data = data
        self.count = count


class _Chain:
    """A query chain whose builder methods all return self and whose
    `.execute()` yields a fixed stub. Covers select/eq/is_/or_/in_/gte/
    order/limit/upsert/update used across notify's queries."""

    def __init__(self, stub: _ExecuteStub):
        self._stub = stub

    def __getattr__(self, _name: str):  # type: ignore[no-untyped-def]
        def _return_self(*_a: Any, **_k: Any) -> _Chain:
            return self

        return _return_self

    def execute(self) -> _ExecuteStub:
        return self._stub


def _synth_user_targets(profiles: list[dict]) -> list[dict]:
    """One active target per profile (`t-<id>`), so each user is relevant to
    their own jobs by default."""
    return [{"user_id": p["user_id"], "target_id": f"t-{p['id']}"} for p in profiles]


def _synth_scores(profiles: list[dict], jobs: list[dict]) -> list[dict]:
    """A scores row per (job × profile) at the job's own score — so the
    per-target best score equals the legacy global job score, preserving
    every threshold test's intent."""
    rows: list[dict] = []
    for job in jobs:
        score = job.get("score")
        if not isinstance(score, int):
            continue
        for p in profiles:
            rows.append(
                {
                    "job_posting_id": job["id"],
                    "target_id": f"t-{p['id']}",
                    "score": score,
                }
            )
    return rows


def _prep_profiles(profiles: list[dict]) -> list[dict]:
    """Give each profile a user_id if the test didn't (relevance keys off it)."""
    for p in profiles:
        p.setdefault("user_id", f"u-{p['id']}")
    return profiles


def _build_supabase_mock(
    profiles: list[dict],
    jobs: list[dict] | None = None,
    claim_response: list[dict] | None = None,
    scores_rows: list[dict] | None = None,
    user_targets_rows: list[dict] | None = None,
) -> MagicMock:
    """Mock answering the email fan-out's reads:
    - user_profiles  → profiles
    - scores         → scores_rows (default: synthesized at each job's score)
    - user_targets   → user_targets_rows (default: one target per profile)
    - notifications_sent: 1st call = claim (upsert), 2nd = update
    """
    profiles = _prep_profiles(profiles)
    jobs = jobs or []
    scores = scores_rows if scores_rows is not None else _synth_scores(profiles, jobs)
    targets = user_targets_rows if user_targets_rows is not None else _synth_user_targets(profiles)

    claim_chain = _Chain(_ExecuteStub(claim_response))
    update_chain = _Chain(_ExecuteStub([]))
    notif_calls = {"n": 0}

    def _notif_table() -> _Chain:
        notif_calls["n"] += 1
        return claim_chain if notif_calls["n"] == 1 else update_chain

    supabase = MagicMock()

    def _table(name: str) -> Any:
        if name == "user_profiles":
            return _Chain(_ExecuteStub(profiles))
        if name == "scores":
            return _Chain(_ExecuteStub(scores))
        if name == "user_targets":
            return _Chain(_ExecuteStub(targets))
        if name == "notifications_sent":
            return _notif_table()
        raise AssertionError(f"Unexpected table: {name}")

    supabase.table.side_effect = _table
    return supabase


def _build_sms_supabase_mock(
    profiles: list[dict],
    jobs: list[dict] | None = None,
    sms_count_today: int = 0,
    claim_response: list[dict] | None = None,
    scores_rows: list[dict] | None = None,
    user_targets_rows: list[dict] | None = None,
) -> MagicMock:
    """SMS variant — notifications_sent flow is count → claim → update."""
    profiles = _prep_profiles(profiles)
    jobs = jobs or []
    scores = scores_rows if scores_rows is not None else _synth_scores(profiles, jobs)
    targets = user_targets_rows if user_targets_rows is not None else _synth_user_targets(profiles)

    count_chain = _Chain(_ExecuteStub([], count=sms_count_today))
    claim_chain = _Chain(_ExecuteStub(claim_response))
    update_chain = _Chain(_ExecuteStub([]))
    notif_calls = {"n": 0}

    def _notif_table() -> _Chain:
        notif_calls["n"] += 1
        if notif_calls["n"] == 1:
            return count_chain
        if notif_calls["n"] == 2:
            return claim_chain
        return update_chain

    supabase = MagicMock()

    def _table(name: str) -> Any:
        if name == "user_profiles":
            return _Chain(_ExecuteStub(profiles))
        if name == "scores":
            return _Chain(_ExecuteStub(scores))
        if name == "user_targets":
            return _Chain(_ExecuteStub(targets))
        if name == "notifications_sent":
            return _notif_table()
        raise AssertionError(f"Unexpected table: {name}")

    supabase.table.side_effect = _table
    return supabase


@pytest.fixture(autouse=True)
def _reset_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    # Default: both env vars set so send path is exercised unless a test clears them.
    monkeypatch.setattr(notify.settings, "next_app_url", "https://example.com")
    monkeypatch.setattr(notify.settings, "job_alert_secret", "test-secret")
    monkeypatch.setattr(notify.settings, "twilio_account_sid", "")
    monkeypatch.setattr(notify.settings, "twilio_auth_token", "")
    monkeypatch.setattr(notify.settings, "twilio_phone_number", "")


# ---------------------------------------------------------------------------
# Email alert tests (#510, #76)
# ---------------------------------------------------------------------------


async def test_returns_zero_when_env_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify.settings, "next_app_url", "")
    supabase = MagicMock()
    jobs: list[dict[str, object]] = [{"id": "j1", "score": 90}]
    assert await notify.send_alerts_for_new_jobs(supabase, jobs) == 0
    supabase.table.assert_not_called()


async def test_returns_zero_when_no_new_jobs() -> None:
    supabase = MagicMock()
    assert await notify.send_alerts_for_new_jobs(supabase, []) == 0
    supabase.table.assert_not_called()


async def test_returns_zero_when_no_active_profiles() -> None:
    supabase = _build_supabase_mock(profiles=[])
    jobs: list[dict[str, object]] = [{"id": "j1", "score": 95}]
    assert await notify.send_alerts_for_new_jobs(supabase, jobs) == 0


async def test_skips_jobs_below_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    # Profile wants score >= 70; the job scored 50 against their target — no send.
    jobs: list[dict[str, object]] = [
        {"id": "j1", "score": 50, "title": "x", "company_name": "y"},
    ]
    supabase = _build_supabase_mock(
        profiles=[{"id": "p1", "email": "me@test", "job_score_threshold": 70}],
        jobs=jobs,
        claim_response=[{"id": "sent-1"}],
    )
    http_client = MagicMock()
    http_client.post = AsyncMock()
    monkeypatch.setattr(notify, "get_http_client", lambda: http_client)

    sent = await notify.send_alerts_for_new_jobs(supabase, jobs)

    assert sent == 0
    http_client.post.assert_not_awaited()


async def test_happy_path_sends_and_patches_external_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs: list[dict[str, object]] = [
        {
            "id": "j1",
            "score": 85,
            "title": "Senior Frontend",
            "company_name": "Acme",
            "location": "Remote",
            "absolute_url": "https://acme.com/jobs/1",
        }
    ]
    supabase = _build_supabase_mock(
        profiles=[{"id": "p1", "email": "me@test", "job_score_threshold": 70}],
        jobs=jobs,
        claim_response=[{"id": "sent-1"}],
    )
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"ok": True, "resendId": "resend-abc"}
    http_client = MagicMock()
    http_client.post = AsyncMock(return_value=response)
    monkeypatch.setattr(notify, "get_http_client", lambda: http_client)

    sent = await notify.send_alerts_for_new_jobs(supabase, jobs)

    assert sent == 1
    http_client.post.assert_awaited_once()
    call = http_client.post.await_args
    assert call is not None
    assert call.args[0] == "https://example.com/api/email/job-alert"
    assert call.kwargs["headers"]["Authorization"] == "Bearer test-secret"
    payload = call.kwargs["json"]
    assert payload["profileId"] == "p1"
    assert payload["to"] == "me@test"
    assert payload["jobId"] == "j1"
    assert payload["score"] == 85


async def test_only_alerts_for_own_target_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#76 acceptance: a job that scored only against user A's target must not
    alert user B, even though the global job score clears B's threshold."""
    jobs: list[dict[str, object]] = [
        {"id": "j1", "score": 90, "title": "x", "company_name": "y"},
    ]
    supabase = _build_supabase_mock(
        profiles=[
            {"id": "pA", "user_id": "uA", "email": "a@test", "job_score_threshold": 70},
            {"id": "pB", "user_id": "uB", "email": "b@test", "job_score_threshold": 70},
        ],
        jobs=jobs,
        claim_response=[{"id": "sent-1"}],
        user_targets_rows=[
            {"user_id": "uA", "target_id": "tA"},
            {"user_id": "uB", "target_id": "tB"},
        ],
        # j1 only scored against A's target tA.
        scores_rows=[{"job_posting_id": "j1", "target_id": "tA", "score": 90}],
    )
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"resendId": "r1"}
    http_client = MagicMock()
    http_client.post = AsyncMock(return_value=response)
    monkeypatch.setattr(notify, "get_http_client", lambda: http_client)

    sent = await notify.send_alerts_for_new_jobs(supabase, jobs)

    assert sent == 1
    http_client.post.assert_awaited_once()
    # The single alert went to A, never B.
    assert http_client.post.await_args.kwargs["json"]["to"] == "a@test"


async def test_no_alert_when_user_has_no_active_target_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A high global score with no scores row for any of the user's targets
    produces no alert."""
    jobs: list[dict[str, object]] = [
        {"id": "j1", "score": 99, "title": "x", "company_name": "y"},
    ]
    supabase = _build_supabase_mock(
        profiles=[{"id": "p1", "email": "me@test", "job_score_threshold": 70}],
        jobs=jobs,
        claim_response=[{"id": "sent-1"}],
        user_targets_rows=[{"user_id": "u-p1", "target_id": "t-p1"}],
        scores_rows=[],  # nothing scored for this user's target
    )
    http_client = MagicMock()
    http_client.post = AsyncMock()
    monkeypatch.setattr(notify, "get_http_client", lambda: http_client)

    sent = await notify.send_alerts_for_new_jobs(supabase, jobs)

    assert sent == 0
    http_client.post.assert_not_awaited()


async def test_dedup_hit_does_not_send(monkeypatch: pytest.MonkeyPatch) -> None:
    # Upsert returns empty data → claim lost, do not send.
    jobs: list[dict[str, object]] = [
        {"id": "j1", "score": 95, "title": "x", "company_name": "y"},
    ]
    supabase = _build_supabase_mock(
        profiles=[{"id": "p1", "email": "me@test", "job_score_threshold": 70}],
        jobs=jobs,
        claim_response=[],
    )
    http_client = MagicMock()
    http_client.post = AsyncMock()
    monkeypatch.setattr(notify, "get_http_client", lambda: http_client)

    sent = await notify.send_alerts_for_new_jobs(supabase, jobs)

    assert sent == 0
    http_client.post.assert_not_awaited()


async def test_upstream_failure_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs: list[dict[str, object]] = [
        {"id": "j1", "score": 95, "title": "x", "company_name": "y"},
    ]
    supabase = _build_supabase_mock(
        profiles=[{"id": "p1", "email": "me@test", "job_score_threshold": 70}],
        jobs=jobs,
        claim_response=[{"id": "sent-1"}],
    )
    response = MagicMock()
    response.status_code = 502
    response.text = "bad gateway"
    http_client = MagicMock()
    http_client.post = AsyncMock(return_value=response)
    monkeypatch.setattr(notify, "get_http_client", lambda: http_client)

    sent = await notify.send_alerts_for_new_jobs(supabase, jobs)

    assert sent == 0
    http_client.post.assert_awaited_once()


# ---------------------------------------------------------------------------
# SMS alert tests (#511, #76)
# ---------------------------------------------------------------------------

_SMS_PROFILE: dict[str, object] = {
    "id": "p1",
    "user_id": "uA",
    "email": "me@test",
    "job_score_threshold": 70,
    "phone_number": "+15551234567",
    "sms_notifications_enabled": True,
    "sms_score_threshold": 85,
    "sms_daily_limit": 5,
}

_HIGH_SCORE_JOB: dict[str, object] = {
    "id": "j1",
    "score": 92,
    "title": "Senior Frontend",
    "company_name": "Acme",
    "absolute_url": "https://acme.com/jobs/1",
}


async def test_sms_skipped_when_twilio_not_configured() -> None:
    supabase = MagicMock()
    jobs: list[dict[str, object]] = [_HIGH_SCORE_JOB]
    # twilio_account_sid is "" by default in fixture
    assert await notify.send_sms_alerts_for_new_jobs(supabase, jobs) == 0
    supabase.table.assert_not_called()


async def test_sms_skipped_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify.settings, "twilio_account_sid", "AC123")
    monkeypatch.setattr(notify.settings, "twilio_auth_token", "token")
    disabled_profile = {**_SMS_PROFILE, "sms_notifications_enabled": False}
    jobs: list[dict[str, object]] = [_HIGH_SCORE_JOB]
    supabase = _build_sms_supabase_mock(profiles=[disabled_profile], jobs=jobs)

    assert await notify.send_sms_alerts_for_new_jobs(supabase, jobs) == 0


async def test_sms_skipped_below_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify.settings, "twilio_account_sid", "AC123")
    monkeypatch.setattr(notify.settings, "twilio_auth_token", "token")
    low_job: dict[str, object] = {**_HIGH_SCORE_JOB, "score": 80}
    supabase = _build_sms_supabase_mock(profiles=[_SMS_PROFILE], jobs=[low_job])

    assert await notify.send_sms_alerts_for_new_jobs(supabase, [low_job]) == 0


async def test_sms_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify.settings, "twilio_account_sid", "AC123")
    monkeypatch.setattr(notify.settings, "twilio_auth_token", "token")
    jobs: list[dict[str, object]] = [_HIGH_SCORE_JOB]
    supabase = _build_sms_supabase_mock(
        profiles=[_SMS_PROFILE],
        jobs=jobs,
        sms_count_today=5,  # at limit
    )

    assert await notify.send_sms_alerts_for_new_jobs(supabase, jobs) == 0


async def test_sms_dedup_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify.settings, "twilio_account_sid", "AC123")
    monkeypatch.setattr(notify.settings, "twilio_auth_token", "token")
    jobs: list[dict[str, object]] = [_HIGH_SCORE_JOB]
    supabase = _build_sms_supabase_mock(
        profiles=[_SMS_PROFILE],
        jobs=jobs,
        sms_count_today=0,
        claim_response=[],  # claim lost
    )

    assert await notify.send_sms_alerts_for_new_jobs(supabase, jobs) == 0


async def test_sms_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify.settings, "twilio_account_sid", "AC123")
    monkeypatch.setattr(notify.settings, "twilio_auth_token", "token")
    monkeypatch.setattr(notify.settings, "twilio_phone_number", "+15559999999")
    jobs: list[dict[str, object]] = [_HIGH_SCORE_JOB]
    supabase = _build_sms_supabase_mock(
        profiles=[_SMS_PROFILE],
        jobs=jobs,
        sms_count_today=2,
        claim_response=[{"id": "sent-1"}],
    )

    with patch("app.services.notify._send_twilio_sms", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = "SM123abc"
        sent = await notify.send_sms_alerts_for_new_jobs(supabase, jobs)

    assert sent == 1
    mock_send.assert_awaited_once()
    call_args = mock_send.await_args
    assert call_args is not None
    assert call_args.args[0] == "+15551234567"
    assert "Senior Frontend" in call_args.args[1]
    assert "Acme" in call_args.args[1]
    assert "92" in call_args.args[1]


async def test_sms_only_alerts_for_own_target_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#76 acceptance (SMS): user B isn't texted about A's role-specific match."""
    monkeypatch.setattr(notify.settings, "twilio_account_sid", "AC123")
    monkeypatch.setattr(notify.settings, "twilio_auth_token", "token")
    jobs: list[dict[str, object]] = [_HIGH_SCORE_JOB]
    profile_b = {
        **_SMS_PROFILE,
        "id": "pB",
        "user_id": "uB",
        "phone_number": "+15550000000",
    }
    supabase = _build_sms_supabase_mock(
        profiles=[profile_b],
        jobs=jobs,
        claim_response=[{"id": "sent-1"}],
        user_targets_rows=[{"user_id": "uB", "target_id": "tB"}],
        scores_rows=[{"job_posting_id": "j1", "target_id": "tA", "score": 92}],
    )

    with patch("app.services.notify._send_twilio_sms", new_callable=AsyncMock) as mock_send:
        sent = await notify.send_sms_alerts_for_new_jobs(supabase, jobs)

    assert sent == 0
    mock_send.assert_not_awaited()


async def test_sms_twilio_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify.settings, "twilio_account_sid", "AC123")
    monkeypatch.setattr(notify.settings, "twilio_auth_token", "token")
    jobs: list[dict[str, object]] = [_HIGH_SCORE_JOB]
    supabase = _build_sms_supabase_mock(
        profiles=[_SMS_PROFILE],
        jobs=jobs,
        sms_count_today=0,
        claim_response=[{"id": "sent-1"}],
    )

    with patch("app.services.notify._send_twilio_sms", new_callable=AsyncMock) as mock_send:
        mock_send.side_effect = RuntimeError("Twilio down")
        sent = await notify.send_sms_alerts_for_new_jobs(supabase, jobs)

    assert sent == 0


# ---------------------------------------------------------------------------
# Per-target notification thresholds (#15)
# ---------------------------------------------------------------------------


def test_qualifying_score_per_target_override_and_fallback() -> None:
    """Per-target override sets the bar; NULL falls back to the profile
    default. Returns the highest score that clears its own target's bar."""
    scores_by_job = {"j1": [("t1", 60), ("t2", 95)]}
    targets = {
        "t1": {"job_score_threshold": 50, "sms_score_threshold": None},  # 60 >= 50 ✓
        "t2": {"job_score_threshold": None, "sms_score_threshold": None},  # 95 >= 90 ✓
    }
    score = notify._qualifying_score("j1", targets, 90, scores_by_job, "job_score_threshold")
    assert score == 95


def test_qualifying_score_override_above_suppresses() -> None:
    scores_by_job = {"j1": [("t1", 60)]}
    targets = {"t1": {"job_score_threshold": 90, "sms_score_threshold": None}}
    # 60 < the per-target 90, even though it clears the profile default 50.
    assert notify._qualifying_score("j1", targets, 50, scores_by_job, "job_score_threshold") is None


def test_qualifying_score_ignores_scores_for_other_users_targets() -> None:
    scores_by_job = {"j1": [("tX", 99)]}  # tX not among this user's active targets
    targets = {"t1": {"job_score_threshold": None, "sms_score_threshold": None}}
    assert notify._qualifying_score("j1", targets, 70, scores_by_job, "job_score_threshold") is None


async def test_per_target_threshold_below_profile_alerts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-target threshold lower than the profile default lets a job that
    wouldn't clear the global bar still alert for that target (#15)."""
    jobs: list[dict[str, object]] = [
        {"id": "j1", "score": 60, "title": "x", "company_name": "y"},
    ]
    supabase = _build_supabase_mock(
        profiles=[{"id": "p1", "user_id": "u1", "email": "me@test", "job_score_threshold": 90}],
        jobs=jobs,
        claim_response=[{"id": "sent-1"}],
        user_targets_rows=[{"user_id": "u1", "target_id": "t1", "job_score_threshold": 50}],
        scores_rows=[{"job_posting_id": "j1", "target_id": "t1", "score": 60}],
    )
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"resendId": "r1"}
    http_client = MagicMock()
    http_client.post = AsyncMock(return_value=response)
    monkeypatch.setattr(notify, "get_http_client", lambda: http_client)

    sent = await notify.send_alerts_for_new_jobs(supabase, jobs)

    assert sent == 1
    http_client.post.assert_awaited_once()


async def test_per_target_threshold_above_profile_suppresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-target threshold higher than the profile default suppresses a job
    that would otherwise clear the global bar."""
    jobs: list[dict[str, object]] = [
        {"id": "j1", "score": 60, "title": "x", "company_name": "y"},
    ]
    supabase = _build_supabase_mock(
        profiles=[{"id": "p1", "user_id": "u1", "email": "me@test", "job_score_threshold": 50}],
        jobs=jobs,
        claim_response=[{"id": "sent-1"}],
        user_targets_rows=[{"user_id": "u1", "target_id": "t1", "job_score_threshold": 90}],
        scores_rows=[{"job_posting_id": "j1", "target_id": "t1", "score": 60}],
    )
    http_client = MagicMock()
    http_client.post = AsyncMock()
    monkeypatch.setattr(notify, "get_http_client", lambda: http_client)

    sent = await notify.send_alerts_for_new_jobs(supabase, jobs)

    assert sent == 0
    http_client.post.assert_not_awaited()


async def test_sms_per_target_threshold_below_profile_alerts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SMS path honors the per-target sms_score_threshold override (#15): a
    score below the profile SMS bar but above the target's override sends."""
    monkeypatch.setattr(notify.settings, "twilio_account_sid", "AC123")
    monkeypatch.setattr(notify.settings, "twilio_auth_token", "token")
    monkeypatch.setattr(notify.settings, "twilio_phone_number", "+15559999999")
    # Profile SMS bar is 85; job scored 80 — below profile, above target's 70.
    low_job: dict[str, object] = {**_HIGH_SCORE_JOB, "score": 80}
    supabase = _build_sms_supabase_mock(
        profiles=[_SMS_PROFILE],
        jobs=[low_job],
        sms_count_today=0,
        claim_response=[{"id": "sent-1"}],
        user_targets_rows=[{"user_id": "uA", "target_id": "t-p1", "sms_score_threshold": 70}],
        scores_rows=[{"job_posting_id": "j1", "target_id": "t-p1", "score": 80}],
    )

    with patch("app.services.notify._send_twilio_sms", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = "SM123"
        sent = await notify.send_sms_alerts_for_new_jobs(supabase, [low_job])

    assert sent == 1
    mock_send.assert_awaited_once()
