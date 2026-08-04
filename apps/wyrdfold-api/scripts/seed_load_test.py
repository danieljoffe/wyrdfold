"""Seed a local Supabase for the #57 load test (``scripts/load_test.py``).

Idempotent. Creates:
- N mock job-board sources (``provider='mock'``) so ``POST /poll`` synthesizes a
  real write herd with zero network — needs ``MOCK_FETCHER_ENABLED=true`` on the
  API (see ``app/services/mock_board.py``).
- One loadtest user (``auth.users`` + ``user_profiles``) whose JWT the rig mints.
- One pipeline-active target the user owns (``app_active`` + an active membership).
- Interactive jobs + graded scores + an optimized experience doc, so the read
  endpoints the rig hammers (``/jobs``, ``/jobs/pipeline-counts``, ``/insights/*``,
  ``/targets/*``, ``/experience/*``) return real data immediately (independent of
  whether a poll has run).

Fixed ids/labels → repeat runs upsert identical rows, so the sync-baseline and
async runs are comparable. Insert order is load-bearing: jobs BEFORE scores (the
``scores_sync_denorm`` trigger reads the job to fill ``job_is_live``).

Run (from ``apps/wyrdfold-api``):

    SUPABASE_URL=http://127.0.0.1:54321 \
    SUPABASE_SERVICE_ROLE_KEY=<LOCAL_SERVICE_ROLE_KEY> \
      uv run python scripts/seed_load_test.py
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from supabase import Client

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.models.experience import OptimizedPayload, Outcome, Role, Skill
from app.models.targets import CategoryProfile, ScoringProfile, TargetCreate
from app.services.targets import crud
from app.supabase_pool import create_service_client

# Titles overlap mock_board._TITLE_STEMS so the LIVE poll path scores the
# synthetic feed instead of excluding it.
_TITLES: tuple[str, ...] = (
    "Customer Experience Manager",
    "Director of Customer Experience",
    "Support Operations Manager",
    "Customer Experience Operations Lead",
    "Head of Customer Support",
    "CX Program Manager",
)
_LOCATIONS: tuple[str, ...] = (
    "Remote, USA",
    "New York, NY",
    "Austin, TX",
    "San Francisco, CA",
    "Chicago, IL",
)
_DESCRIPTION_HTML = (
    "<h2>About the role</h2><p>Own the end-to-end customer experience program: "
    "partner with support operations, product, and data to improve CSAT and scale "
    "voice-of-customer loops.</p><h2>What we're looking for</h2><ul>"
    "<li>7+ years in customer experience or support operations.</li>"
    "<li>Experience with Zendesk / Salesforce Service Cloud.</li></ul>"
)
_TARGET_LABEL = "Customer Experience Leadership (loadtest)"


def _client() -> Client:
    sb = create_service_client()
    if sb is None:
        raise SystemExit(
            "Supabase not configured — set SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY"
        )
    return sb


def _seed_sources(sb: Client, n: int, jobs_per_source: int) -> list[Any]:
    """Upsert N mock sources (idempotent on board_token). The ``:N`` token
    suffix is the per-source synthetic job count (mock_board._job_count)."""
    rows: list[dict[str, Any]] = [
        {
            "provider": "mock",
            "board_token": f"loadtest-{i:02d}:{jobs_per_source}",
            "company_name": f"Loadtest Mock Co {i:02d}",
            "enabled": True,
            "last_polled_at": None,  # NULL => immediately poll-due
        }
        for i in range(n)
    ]
    sb.table("sources").upsert(rows, on_conflict="board_token").execute()
    got = (
        sb.table("sources")
        .select("id, board_token")
        .like("board_token", "loadtest-%")
        .order("board_token")
        .execute()
    )
    return got.data


def _get_or_create_user(sb: Client, email: str) -> str:
    """Create the auth user (email pre-confirmed) or find the existing one."""
    try:
        resp = sb.auth.admin.create_user({"email": email, "email_confirm": True})
        return resp.user.id
    except Exception:
        users = sb.auth.admin.list_users()
        users = getattr(users, "users", users)  # tolerate a paginated wrapper
        for u in users:
            if getattr(u, "email", None) == email:
                return u.id
        raise SystemExit(f"could not create or find auth user {email!r}") from None


def _seed_user_profile(sb: Client, uid: str, email: str) -> None:
    sb.table("user_profiles").upsert(
        {"user_id": uid, "email": email, "name": "Load Test"},
        on_conflict="user_id",
    ).execute()


def _seed_target(sb: Client, uid: str) -> str:
    """Find-or-create the target, mark it app-active AND link an active
    membership — either arm makes it pipeline-active (crud.is_pipeline_active)."""
    profile = ScoringProfile(
        categories={
            "cx": CategoryProfile(
                keywords={"customer experience": 3, "support operations": 2, "cx": 2},
            )
        }
    )
    target = crud.create(
        sb,
        TargetCreate(
            label=_TARGET_LABEL,
            scoring_profile=profile,
            search_keywords=["customer experience", "support operations", "cx"],
        ),
    )
    crud.set_app_active(sb, target.id)
    crud.link_user_to_target(
        sb,
        user_id=uid,
        target_id=target.id,
        is_active=True,
        fit_score=90,
        fit_score_reasoning="loadtest seed",
        enforce_active_limit=False,  # bypass MAX_ACTIVE_TARGETS_PER_USER
    )
    return target.id


def _seed_jobs_and_scores(sb: Client, source_id: str, target_id: str, n: int) -> int:
    """Upsert N live/US jobs, then a graded 'complete' score for each against the
    target. Jobs are committed first so the scores denorm trigger sees them."""
    jobs: list[dict[str, Any]] = [
        {
            "external_id": f"loadtest-manual-{i:04d}",
            "source_id": source_id,
            "title": f"{_TITLES[i % len(_TITLES)]} {i}",
            "company_name": f"Loadtest Mock Co {i % 10:02d}",
            "location": _LOCATIONS[i % len(_LOCATIONS)],
            "description_html": _DESCRIPTION_HTML,
            "absolute_url": f"https://example.com/loadtest/jobs/{i}",
            "is_us": True,
            "is_genuine_role": True,  # non-genuine rows get archived out of the list
            "is_remote": True,
            "city": "Remote",
            "country": "US",
            "location_remote": True,
            "salary_min": 150000,
            "salary_max": 190000,
            "salary_currency": "USD",
            "salary_period": "year",
            "source_posted_at": "2026-07-01T00:00:00Z",
            # role_family left NULL => off-family gate is a no-op; archived_at /
            # purged_at NULL => live.
        }
        for i in range(n)
    ]
    job_resp = sb.table("jobs").upsert(jobs, on_conflict="external_id,source_id").execute()
    job_ids = [row["id"] for row in cast("list[dict[str, Any]]", job_resp.data)]

    now = datetime.now(UTC).isoformat()
    scores: list[dict[str, Any]] = []
    for i, job_id in enumerate(job_ids):
        s = 70 + (i % 30)  # 70..99
        scores.append(
            {
                "job_posting_id": job_id,
                "target_id": target_id,
                "score": s,
                "score_breakdown": {"total": s, "matched_keywords": ["customer experience"]},
                "excluded": False,
                "scoring_status": "complete",
                "scored_profile_version": 1,
                "promising": True,
                "phase1_confidence": 90,
                "axis_scores": {
                    "title_fit": s,
                    "skills_fit": max(0, s - 2),
                    "seniority_fit": max(0, s - 1),
                    "domain_fit": max(0, s - 3),
                },
                "fit_reasoning": "Strong CX / support-ops leadership match.",
                "recency_score": s,
                "updated_at": now,
                # job_is_live / job_role_family / job_first_seen_at / is_graded
                # are trigger-owned — do NOT set them here.
            }
        )
    sb.table("scores").upsert(scores, on_conflict="job_posting_id,target_id").execute()
    return len(job_ids)


def _seed_experience(sb: Client, uid: str) -> None:
    """Seed one optimized doc (powers /experience/optimized + gap-health).
    No natural unique key, so delete-then-insert for this user (idempotent)."""
    payload = OptimizedPayload(
        summary="Customer experience and support-operations leader, 10+ years.",
        roles=[
            Role(
                id="r1",
                company="Acme",
                title="Director, Customer Experience",
                start="2019",
                skills=["customer experience", "support operations"],
                outcome_refs=["Cut time-to-resolution 40%"],
            )
        ],
        skills=[
            Skill(name="Customer Experience", years=10.0),
            Skill(name="Support Operations", years=8.0),
        ],
        outcomes=[
            Outcome(
                description="Cut time-to-resolution 40%",
                metric="TTR",
                value="-40%",
                role_ref="r1",
            )
        ],
    )
    sb.table("experience_optimized_docs").delete().eq("user_id", uid).execute()
    sb.table("experience_optimized_docs").insert(
        {
            "user_id": uid,
            "prose_doc_id": None,
            "version": 1,
            "payload": payload.model_dump(mode="json"),
            "source": "llm",
        }
    ).execute()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-email", default="loadtest@example.com")
    parser.add_argument("--sources", type=int, default=10, help="mock source count")
    parser.add_argument(
        "--jobs-per-source",
        type=int,
        default=200,
        help="synthetic jobs each mock source yields on POST /poll (capped 2000)",
    )
    parser.add_argument(
        "--interactive-jobs",
        type=int,
        default=60,
        help="directly-seeded jobs+scores for the read endpoints",
    )
    args = parser.parse_args()

    sb = _client()
    sources = _seed_sources(sb, args.sources, args.jobs_per_source)
    if not sources:
        raise SystemExit("no mock sources seeded")
    interactive_source_id = sources[0]["id"]
    uid = _get_or_create_user(sb, args.user_email)
    _seed_user_profile(sb, uid, args.user_email)
    target_id = _seed_target(sb, uid)
    n_jobs = _seed_jobs_and_scores(sb, interactive_source_id, target_id, args.interactive_jobs)
    _seed_experience(sb, uid)

    herd = args.sources * args.jobs_per_source
    print("seeded:")
    print(f"  mock sources     : {len(sources)}  (~{herd} synthetic jobs on POST /poll)")
    print(f"  user             : {uid}  <{args.user_email}>")
    print(f"  target (active)  : {target_id}")
    print(f"  jobs + scores    : {n_jobs}")
    print("  optimized doc    : v1")


if __name__ == "__main__":
    main()
