"""Email and SMS alerts for newly discovered high-scoring jobs.

Supports issues #510 (email) and #511 (SMS).

Email flow:
    poller.py  (FastAPI)  ──POST──▶  /api/email/job-alert  (Next.js)
         │                                       │
         │                                       └─ renders React Email, sends via Resend
         └─ writes notifications_sent (dedup, channel='email')

SMS flow:
    poller.py  (FastAPI)  ──Twilio SDK──▶  Twilio API
         │
         └─ writes notifications_sent (dedup, channel='sms')

At-most-once semantics: a dedup row is claimed via upsert-with-
ignore_duplicates BEFORE the send. If the claim wins, we send; if the
send fails, the row persists and no retry ever fires. This trades a
missed notification for guaranteed non-duplication, which is the right
choice for a personal job alert.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
from supabase import Client

from app.config import settings
from app.http_client import get_http_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Email alerts (#510)
# ---------------------------------------------------------------------------


async def send_alerts_for_new_jobs(supabase: Client, new_job_rows: list[dict[str, Any]]) -> int:
    """Fan out email alerts for each (profile × new-job) pair where the job
    scored above the profile's threshold *against one of that user's own
    targets* (#76).

    Relevance is driven by ``scores ⋈ user_targets``, never the vestigial
    global ``jobs.score`` — so on a shared instance a user is never alerted
    about a posting that only matched *another* user's role. Returns the
    count of alerts actually sent.
    """
    if not new_job_rows:
        return 0
    if not settings.next_app_url or not settings.job_alert_secret:
        logger.debug("Job alerts skipped: NEXT_APP_URL and JOB_ALERT_SECRET must both be set")
        return 0

    profiles = await _fetch_active_profiles(supabase)
    if not profiles:
        return 0

    job_ids = [j["id"] for j in new_job_rows if isinstance(j.get("id"), str)]
    scores_by_job = await _load_scores_by_job(supabase, job_ids)
    targets_by_user = await _active_targets_by_user(
        supabase, [p["user_id"] for p in profiles if p.get("user_id")]
    )

    sent = 0
    for job in new_job_rows:
        job_id = job.get("id")
        if not isinstance(job_id, str):
            continue
        for profile in profiles:
            user_targets = targets_by_user.get(profile.get("user_id") or "", {})
            score = _qualifying_score(
                job_id,
                user_targets,
                int(profile.get("job_score_threshold", 100)),
                scores_by_job,
                "job_score_threshold",
            )
            if score is None:
                continue
            if await _try_send_one(supabase, profile, job, score):
                sent += 1
    return sent


async def _load_scores_by_job(
    supabase: Client, job_ids: list[str]
) -> dict[str, list[tuple[str, int]]]:
    """Per-job ``(target_id, score)`` pairs for the given jobs, dropping rows
    the scorer flagged ``excluded``. Drives per-recipient relevance (#76)."""
    if not job_ids:
        return {}
    resp = await asyncio.to_thread(
        lambda: (
            supabase.table("scores")
            .select("job_posting_id, target_id, score")
            .in_("job_posting_id", job_ids)
            .eq("excluded", False)
            .execute()
        )
    )
    by_job: dict[str, list[tuple[str, int]]] = {}
    for row in cast(list[dict[str, Any]], resp.data or []):
        job_id = row.get("job_posting_id")
        target_id = row.get("target_id")
        score = row.get("score")
        if isinstance(job_id, str) and isinstance(target_id, str) and isinstance(score, int):
            by_job.setdefault(job_id, []).append((target_id, score))
    return by_job


async def _active_targets_by_user(
    supabase: Client, user_ids: list[str]
) -> dict[str, dict[str, dict[str, int | None]]]:
    """Per user, the active ``target_id`` → per-target notification thresholds
    map, via the ``user_targets`` junction (#15).

    Each target carries its own ``job_score_threshold`` / ``sms_score_threshold``
    (NULL ⇒ fall back to the user-profile default). The presence of a
    ``target_id`` key also marks it as one of the user's active targets — the
    relevance gate that ``_qualifying_score`` keys on.
    """
    if not user_ids:
        return {}
    resp = await asyncio.to_thread(
        lambda: (
            supabase.table("user_targets")
            .select("user_id, target_id, job_score_threshold, sms_score_threshold")
            .in_("user_id", user_ids)
            .eq("is_active", True)
            .execute()
        )
    )
    by_user: dict[str, dict[str, dict[str, int | None]]] = {}
    for row in cast(list[dict[str, Any]], resp.data or []):
        user_id = row.get("user_id")
        target_id = row.get("target_id")
        if isinstance(user_id, str) and isinstance(target_id, str):
            by_user.setdefault(user_id, {})[target_id] = {
                "job_score_threshold": row.get("job_score_threshold"),
                "sms_score_threshold": row.get("sms_score_threshold"),
            }
    return by_user


def _qualifying_score(
    job_id: str,
    user_targets: dict[str, dict[str, int | None]],
    profile_default: int,
    scores_by_job: dict[str, list[tuple[str, int]]],
    threshold_key: str,
) -> int | None:
    """The score to alert on for this (user, job), or ``None`` to not alert.

    For each of the user's active targets the job scored against, the bar is
    that target's *effective* threshold — its per-target override
    (``user_targets.<threshold_key>``) when set, else ``profile_default``
    (#15). Returns the highest score that clears its target's bar.

    With every per-target threshold NULL this reduces to the legacy rule
    ("highest score across the user's targets, alert if it clears the profile
    threshold"): the qualifying max equals the overall max exactly when that
    max clears the bar. So dormant per-target columns preserve today's
    behavior — the relevance set (active targets) is unchanged either way.
    """
    if not user_targets:
        return None
    best: int | None = None
    for target_id, score in scores_by_job.get(job_id, []):
        overrides = user_targets.get(target_id)
        if overrides is None:
            continue  # not one of this user's active targets
        override = overrides.get(threshold_key)
        effective = profile_default if override is None else override
        if score >= effective and (best is None or score > best):
            best = score
    return best


async def _fetch_active_profiles(supabase: Client) -> list[dict[str, Any]]:
    query = (
        supabase.table("user_profiles")
        .select(
            "id, user_id, email, job_score_threshold,"
            " phone_number, sms_notifications_enabled,"
            " sms_score_threshold, sms_daily_limit"
        )
        .eq("job_notifications_enabled", True)
        .is_("unsubscribed_at", "null")
    )
    # Alert hygiene: abandoned users stop receiving job alerts even
    # before the lifecycle sweep deactivates their targets. NULL
    # last_seen_at passes (or-filter) — never punish missing data.
    if settings.idle_deactivate_days > 0:
        cutoff = (datetime.now(UTC) - timedelta(days=settings.idle_deactivate_days)).isoformat()
        query = query.or_(f"last_seen_at.gte.{cutoff},last_seen_at.is.null")
    resp = await asyncio.to_thread(query.execute)
    return cast(list[dict[str, Any]], resp.data or [])


async def _try_send_one(
    supabase: Client,
    profile: dict[str, Any],
    job: dict[str, Any],
    score: int,
) -> bool:
    profile_id = profile["id"]
    job_id = job["id"]

    claim = await asyncio.to_thread(
        lambda: (
            supabase.table("notifications_sent")
            .upsert(
                {
                    "user_profile_id": profile_id,
                    "job_posting_id": job_id,
                    "score_at_send": score,
                    "channel": "email",
                },
                on_conflict="user_profile_id,job_posting_id,channel",
                ignore_duplicates=True,
            )
            .execute()
        )
    )
    claimed_rows = claim.data or []
    if not claimed_rows:
        return False
    claim_id = cast(dict[str, Any], claimed_rows[0])["id"]

    try:
        resend_id = await _post_alert(profile, job, score)
    except Exception:
        logger.exception("Job alert POST raised for profile=%s job=%s", profile_id, job_id)
        return False

    if resend_id:
        await asyncio.to_thread(
            lambda: (
                supabase.table("notifications_sent")
                .update({"external_id": resend_id})
                .eq("id", claim_id)
                .execute()
            )
        )
        return True

    return False


async def _post_alert(profile: dict[str, Any], job: dict[str, Any], score: int) -> str | None:
    payload = {
        "profileId": profile["id"],
        "to": profile["email"],
        "jobId": job["id"],
        "title": job.get("title") or "",
        "company": job.get("company_name") or "",
        "location": job.get("location"),
        "score": score,
        "jobUrl": job.get("absolute_url") or "",
    }
    url = f"{settings.next_app_url.rstrip('/')}/api/email/job-alert"
    client: httpx.AsyncClient = get_http_client()
    resp = await client.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {settings.job_alert_secret}"},
    )
    if resp.status_code != 200:
        logger.warning(
            "Job alert POST failed: status=%s body=%s",
            resp.status_code,
            resp.text[:200],
        )
        return None
    body = resp.json()
    resend_id = body.get("resendId")
    return resend_id if isinstance(resend_id, str) else None


# ---------------------------------------------------------------------------
# Idle-lifecycle "target paused" email
# ---------------------------------------------------------------------------


async def send_target_paused_email(
    supabase: Client, *, user_id: str, target_labels: list[str]
) -> bool:
    """One email telling an idle user their target(s) were auto-paused.

    Called by the lifecycle sweep only for rows it just transitioned, so
    at-most-once holds without a dedup table (a crash between flip and
    send loses the email — same trade as job alerts). Honors the same
    opt-out signals as job alerts.
    """
    if not settings.next_app_url or not settings.job_alert_secret:
        return False

    resp = await asyncio.to_thread(
        lambda: (
            supabase.table("user_profiles")
            .select("id, email, job_notifications_enabled, unsubscribed_at")
            .eq("user_id", user_id)
            .execute()
        )
    )
    rows = cast(list[dict[str, Any]], resp.data or [])
    if not rows:
        return False
    profile = rows[0]
    if not profile.get("job_notifications_enabled") or profile.get("unsubscribed_at"):
        return False

    payload = {
        "profileId": profile["id"],
        "to": profile["email"],
        "targetLabels": target_labels,
        "idleDays": settings.idle_deactivate_days,
    }
    url = f"{settings.next_app_url.rstrip('/')}/api/email/target-paused"
    client: httpx.AsyncClient = get_http_client()
    try:
        post_resp = await client.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {settings.job_alert_secret}"},
        )
    except Exception:
        logger.exception("Target-paused email POST raised for user %s", user_id)
        return False
    if post_resp.status_code != 200:
        logger.warning(
            "Target-paused email POST failed: status=%s body=%s",
            post_resp.status_code,
            post_resp.text[:200],
        )
        return False
    return True


# ---------------------------------------------------------------------------
# SMS alerts (#511)
# ---------------------------------------------------------------------------


async def send_sms_alerts_for_new_jobs(supabase: Client, new_job_rows: list[dict[str, Any]]) -> int:
    """Fan out SMS alerts for each (profile × new-job) pair that clears the
    per-profile SMS threshold and daily rate limit. Returns the count sent.
    """
    if not new_job_rows:
        return 0
    if not settings.twilio_account_sid or not settings.twilio_auth_token:
        logger.debug("SMS alerts skipped: Twilio credentials not configured")
        return 0

    profiles = await _fetch_active_profiles(supabase)
    if not profiles:
        return 0

    # Filter to SMS-enabled profiles with a phone number
    sms_profiles = [
        p for p in profiles if p.get("sms_notifications_enabled") and p.get("phone_number")
    ]
    if not sms_profiles:
        return 0

    job_ids = [j["id"] for j in new_job_rows if isinstance(j.get("id"), str)]
    scores_by_job = await _load_scores_by_job(supabase, job_ids)
    targets_by_user = await _active_targets_by_user(
        supabase, [p["user_id"] for p in sms_profiles if p.get("user_id")]
    )

    sent = 0
    for job in new_job_rows:
        job_id = job.get("id")
        if not isinstance(job_id, str):
            continue
        for profile in sms_profiles:
            user_targets = targets_by_user.get(profile.get("user_id") or "", {})
            score = _qualifying_score(
                job_id,
                user_targets,
                int(profile.get("sms_score_threshold", 100)),
                scores_by_job,
                "sms_score_threshold",
            )
            if score is None:
                continue
            if await _try_send_sms(supabase, profile, job, score):
                sent += 1
    return sent


async def _sms_count_today(supabase: Client, profile_id: str) -> int:
    """Count SMS notifications sent today for a profile."""
    today = datetime.now(UTC).strftime("%Y-%m-%dT00:00:00+00:00")
    resp = await asyncio.to_thread(
        lambda: (
            supabase.table("notifications_sent")
            .select("id", count="exact")  # type: ignore[arg-type]
            .eq("user_profile_id", profile_id)
            .eq("channel", "sms")
            .gte("sent_at", today)
            .execute()
        )
    )
    return resp.count or 0


async def _try_send_sms(
    supabase: Client,
    profile: dict[str, Any],
    job: dict[str, Any],
    score: int,
) -> bool:
    profile_id = profile["id"]
    job_id = job["id"]
    daily_limit = int(profile.get("sms_daily_limit", 5))

    # Rate limit check
    today_count = await _sms_count_today(supabase, profile_id)
    if today_count >= daily_limit:
        logger.debug(
            "SMS rate limited for profile=%s (sent=%d, limit=%d)",
            profile_id,
            today_count,
            daily_limit,
        )
        return False

    # Claim dedup row
    claim = await asyncio.to_thread(
        lambda: (
            supabase.table("notifications_sent")
            .upsert(
                {
                    "user_profile_id": profile_id,
                    "job_posting_id": job_id,
                    "score_at_send": score,
                    "channel": "sms",
                },
                on_conflict="user_profile_id,job_posting_id,channel",
                ignore_duplicates=True,
            )
            .execute()
        )
    )
    claimed_rows = claim.data or []
    if not claimed_rows:
        return False
    claim_id = cast(dict[str, Any], claimed_rows[0])["id"]

    # Build SMS body
    title = job.get("title") or "New role"
    company = job.get("company_name") or ""
    deep_link = ""
    if settings.next_app_url:
        deep_link = f" {settings.next_app_url.rstrip('/')}/fitted/jobs/{job_id}"
    body = f"Great match: {title}"
    if company:
        body += f" at {company}"
    body += f" (score: {score}).{deep_link}"

    try:
        twilio_sid = await _send_twilio_sms(profile["phone_number"], body)
    except Exception:
        logger.exception("Twilio SMS raised for profile=%s job=%s", profile_id, job_id)
        return False

    if twilio_sid:
        await asyncio.to_thread(
            lambda: (
                supabase.table("notifications_sent")
                .update({"external_id": twilio_sid})
                .eq("id", claim_id)
                .execute()
            )
        )
        return True

    return False


_twilio_client: Any = None


def _get_twilio_client() -> Any:
    """Return a cached Twilio client, creating it on first call."""
    global _twilio_client
    if _twilio_client is None:
        from twilio.rest import Client as TwilioClient  # type: ignore[import-untyped]

        _twilio_client = TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)
    return _twilio_client


async def _send_twilio_sms(to: str, body: str) -> str | None:
    """Send an SMS via Twilio. Returns the message SID on success."""
    client = _get_twilio_client()

    message = await asyncio.to_thread(
        lambda: client.messages.create(
            body=body,
            from_=settings.twilio_phone_number,
            to=to,
        )
    )
    sid: str | None = message.sid
    return sid
