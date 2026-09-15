"""Persona definitions for staging. Drafted here, placed once #1043 merges.

Each persona differs along BOTH axes the owner asked for:
  * account status  — plan, onboarding stage, LLM posture, notifications
  * resume background — the OptimizedPayload that actually drives fit scoring

The second one is the subtle part. `derive_fit_score()` takes an
OptimizedPayload (summary/roles/skills), NOT `uploaded_resumes.extracted_text`,
so seeding only the upload row would give five personas identical matching
behaviour — five accounts that look different and score the same.
"""

from typing import Any

# plan values come from entitlements.Plan: free | trial | starter | pro
# Typed rather than inferred: mypy widens a mixed-value dict literal to
# `object`, which makes every field access downstream an error.
PERSONAS: list[dict[str, Any]] = [
    {
        "email": "priya.raghavan@example.com",
        "name": "Priya Raghavan",
        "location": "Austin, TX",
        "plan": "pro",
        "onboarded": True,
        "onboarding_path": "A",
        "llm_enabled": True,
        "job_score_threshold": 85,
        "notes": "power user: highest tier, strict threshold, deep backend history",
        "payload": {
            "summary": (
                "Staff backend engineer with nine years building distributed "
                "systems in Go and Python. Owns data-plane services at "
                "six-figure RPS; led two Postgres migrations with zero planned "
                "downtime."
            ),
            "roles": [
                {
                    "id": "r1",
                    "company": "Northwind Data",
                    "title": "Staff Backend Engineer",
                    "start": "2022-03",
                    "end": None,
                    "summary": "Owns the ingestion data plane and its Postgres storage layer.",
                    "skills": ["Go", "PostgreSQL", "Kafka", "Kubernetes"],
                    "outcome_refs": ["Cut p99 ingest latency from 840ms to 96ms"],
                },
                {
                    "id": "r2",
                    "company": "Cedar Logistics",
                    "title": "Senior Software Engineer",
                    "start": "2018-06",
                    "end": "2022-02",
                    "summary": "Routing and dispatch services for a national fleet.",
                    "skills": ["Python", "PostgreSQL", "gRPC"],
                    "outcome_refs": ["Migrated 4TB of routing data with no planned downtime"],
                },
            ],
            "skills": [
                {"name": "Go", "years": 7.0, "evidence_refs": ["r1"]},
                {"name": "PostgreSQL", "years": 9.0, "evidence_refs": ["r1", "r2"]},
                {"name": "Kubernetes", "years": 5.0, "evidence_refs": ["r1"]},
                {"name": "Distributed systems", "years": 8.0, "evidence_refs": ["r1", "r2"]},
            ],
            "outcomes": [
                {
                    "description": "Cut p99 ingest latency from 840ms to 96ms",
                    "metric": "p99 latency",
                    "value": "840ms -> 96ms",
                    "role_ref": "r1",
                },
                {
                    "description": "Migrated 4TB of routing data with no planned downtime",
                    "metric": "downtime",
                    "value": "0 minutes",
                    "role_ref": "r2",
                },
            ],
        },
    },
    {
        "email": "marcus.bell@example.com",
        "name": "Marcus Bell",
        "location": "Remote (US)",
        "plan": "starter",
        "onboarded": True,
        "onboarding_path": "B",
        "llm_enabled": True,
        "job_score_threshold": 60,
        "notes": "early career: low threshold, wants volume; opposite end from Priya",
        "payload": {
            "summary": (
                "Data analyst, eighteen months in, strongest in SQL and dbt. "
                "Built the reporting layer a twelve-person go-to-market team "
                "runs on."
            ),
            "roles": [
                {
                    "id": "r1",
                    "company": "Harborline",
                    "title": "Data Analyst",
                    "start": "2025-02",
                    "end": None,
                    "summary": "Owns GTM reporting and the dbt models behind it.",
                    "skills": ["SQL", "dbt", "Python", "Looker"],
                    "outcome_refs": [
                        "Replaced 30+ hand-maintained spreadsheets with 14 dbt models"
                    ],
                },
            ],
            "skills": [
                {"name": "SQL", "years": 2.0, "evidence_refs": ["r1"]},
                {"name": "dbt", "years": 1.5, "evidence_refs": ["r1"]},
                {"name": "Python", "years": 1.5, "evidence_refs": ["r1"]},
            ],
            "outcomes": [
                {
                    "description": "Replaced 30+ hand-maintained spreadsheets with 14 dbt models",
                    "metric": "manual reports retired",
                    "value": "30+",
                    "role_ref": "r1",
                },
            ],
        },
    },
    {
        "email": "yuki.tanaka@example.com",
        "name": "Yuki Tanaka",
        "location": "Seattle, WA",
        "plan": "trial",
        "trial_age_days": 1,  # inside the 3-day window
        "onboarded": False,  # MID-WIZARD: completed_at NULL
        "onboarding_current_step": "completion",
        "llm_enabled": True,
        "job_score_threshold": 70,
        "notes": "exercises the onboarding wizard's resume state",
        "payload": {
            "summary": (
                "Product manager, six years in B2B SaaS. Shipped billing and "
                "entitlements for a seat-based product through two pricing "
                "changes."
            ),
            "roles": [
                {
                    "id": "r1",
                    "company": "Fathom Software",
                    "title": "Senior Product Manager",
                    "start": "2021-08",
                    "end": None,
                    "summary": "Owns billing, entitlements and the upgrade path.",
                    "skills": ["Product strategy", "Pricing", "SQL"],
                    "outcome_refs": ["Raised trial-to-paid conversion from 4.1% to 7.8%"],
                },
            ],
            "skills": [
                {"name": "Product strategy", "years": 6.0, "evidence_refs": ["r1"]},
                {"name": "Pricing and packaging", "years": 4.0, "evidence_refs": ["r1"]},
                {"name": "SQL", "years": 3.0, "evidence_refs": ["r1"]},
            ],
            "outcomes": [
                {
                    "description": "Raised trial-to-paid conversion from 4.1% to 7.8%",
                    "metric": "trial->paid",
                    "value": "4.1% -> 7.8%",
                    "role_ref": "r1",
                },
            ],
        },
    },
    {
        "email": "dana.okonkwo@example.com",
        "name": "Dana Okonkwo",
        "location": "Chicago, IL",
        "plan": "trial",
        "trial_age_days": 365,  # EXPIRED: exercises trial_expired()
        "onboarded": True,
        "onboarding_path": "C",
        "llm_enabled": True,
        "job_score_threshold": 70,
        "notes": "expired trial + career changer: weak keyword overlap on purpose",
        "payload": {
            "summary": (
                "Secondary school teacher of nine years moving into UX "
                "research. Certificate in human-centred design; two "
                "volunteer research engagements."
            ),
            "roles": [
                {
                    "id": "r1",
                    "company": "Lakeside Public Schools",
                    "title": "Teacher, Physics",
                    "start": "2016-08",
                    "end": "2025-06",
                    "summary": "Classroom instruction and curriculum design.",
                    "skills": ["Curriculum design", "Facilitation", "Assessment"],
                    "outcome_refs": ["Redesigned the physics curriculum for 400 students"],
                },
                {
                    "id": "r2",
                    "company": "Civic Design Collective",
                    "title": "Volunteer UX Researcher",
                    "start": "2025-01",
                    "end": None,
                    "summary": "Interview-based research for municipal service redesign.",
                    "skills": ["User interviews", "Synthesis", "Usability testing"],
                    "outcome_refs": ["Ran 22 interviews informing a benefits-portal redesign"],
                },
            ],
            "skills": [
                {"name": "User interviews", "years": 1.0, "evidence_refs": ["r2"]},
                {"name": "Curriculum design", "years": 9.0, "evidence_refs": ["r1"]},
                {"name": "Facilitation", "years": 9.0, "evidence_refs": ["r1", "r2"]},
            ],
            "outcomes": [
                {
                    "description": "Ran 22 interviews informing a benefits-portal redesign",
                    "metric": "interviews",
                    "value": "22",
                    "role_ref": "r2",
                },
                {
                    "description": "Redesigned the physics curriculum for 400 students",
                    "metric": "students",
                    "value": "400",
                    "role_ref": "r1",
                },
            ],
        },
    },
    {
        "email": "sam.ortiz@example.com",
        "name": "Sam Ortiz",
        "location": "Denver, CO",
        "plan": "free",  # BYOK
        "onboarded": True,
        "onboarding_path": "A",
        "llm_enabled": False,  # LLM features off
        "unsubscribed": True,  # notifications off
        "job_score_threshold": 75,
        "notes": "BYOK + LLM disabled + unsubscribed: the all-off path",
        "payload": {
            "summary": (
                "Platform engineer, twelve years. Build systems, CI, and "
                "developer tooling for large monorepos."
            ),
            "roles": [
                {
                    "id": "r1",
                    "company": "Ridgeway Systems",
                    "title": "Principal Platform Engineer",
                    "start": "2019-04",
                    "end": None,
                    "summary": "Owns CI and the build graph for a 2M-line monorepo.",
                    "skills": ["Bazel", "CI/CD", "Go", "Terraform"],
                    "outcome_refs": ["Cut median CI wall time from 41 to 9 minutes"],
                },
            ],
            "skills": [
                {"name": "CI/CD", "years": 12.0, "evidence_refs": ["r1"]},
                {"name": "Bazel", "years": 6.0, "evidence_refs": ["r1"]},
                {"name": "Terraform", "years": 7.0, "evidence_refs": ["r1"]},
            ],
            "outcomes": [
                {
                    "description": "Cut median CI wall time from 41 to 9 minutes",
                    "metric": "CI wall time",
                    "value": "41m -> 9m",
                    "role_ref": "r1",
                },
            ],
        },
    },
]
