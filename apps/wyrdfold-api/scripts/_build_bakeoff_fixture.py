"""Build a hard-adjacent bake-off fixture for the Phase-1 triage eval.

Real engineering titles pulled from prod (the boundary space where models
actually diverge for a Frontend target: generic SWE vs frontend vs
backend/platform/data/mobile/security, plus free-gate false positives). No
expected_promising labels — sonnet is the oracle (agreement report), so we
measure how well haiku (incumbent) and gemini-flash (candidate) track sonnet.
"""

import json
from pathlib import Path

FIXTURE = Path(__file__).parent.parent / "tests" / "fixtures" / "eval_set.json"

# From prod `jobs` (distinct engineering titles, most-frequent first).
RAW_TITLES = [
    "Data Scientist",
    "Senior Data Scientist",
    "DevOps Engineer",
    "Data Analyst",
    "Senior DevOps Engineer",
    "Senior Software Engineer",
    "Software Engineer",
    "Senior Data Analyst",
    "Senior Platform Engineer",
    "Frontend Engineer",
    "Staff Data Scientist",
    "Senior Frontend Engineer",
    "Platform Engineer",
    "Staff Software Engineer",
    "Senior Machine Learning Engineer",
    "Solutions Architect",
    "Senior Android Engineer",
    "Lead Data Scientist",
    "Senior Solutions Architect",
    "Senior Solutions Engineer",
    "Principal Software Engineer",
    "Principal Data Scientist",
    "Solutions Engineer",
    "Senior Data Platform Engineer",
    "Senior iOS Engineer",
    "GTM Engineer",
    "Software Engineer, Backend",
    "Data Analyst II",
    "Software Engineer Intern",
    "Data Engineer",
    "Senior Software Engineer, Data Platform",
    "iOS Engineer",
    "Senior Solution Engineer",
    "Forward Deployed Engineer",
    "Lead Data Analyst",
    "Senior Software Engineer, Backend",
    "Senior Forward Deployed Engineer",
    "Site Reliability Engineer",
    "Security Engineer",
    "Data Science Analyst",
    "Staff Software Engineer, Frontend",
    "Staff Data Analyst",
    "Machine Learning Engineer",
    "Principal DevOps Engineer",
    "Senior Security Engineer",
    "Senior Performance Engineer",
    "Senior Software Engineer, Frontend",
    "Sales Manager - Data Platforms",
    "Data Scientist II",
    "Senior UX Designer",
    "Senior Software Engineer - Backend",
    "UI Engineer",
    "Product Security Engineer",
    "Legal Engineer Associate",
    "Lead Design Engineer",
    "Android Engineer",
    "Associate Software Engineer",
    "Principal Design Engineer",
    "Senior Quality Development Engineer",
    "Software Engineer, Platform",
    "Lead Legal Engineer",
    "Staff Software Engineer - Backend",
    "Technical Support Engineer",
    "Staff Frontend Engineer",
    "Senior Analytics Engineer",
    "Staff DevOps Engineer",
    "Senior Site Reliability Engineer",
    "Staff Software Engineer - Distributed Data Systems",
    "Marketing Data Analyst",
    "Senior Software Engineer, DevOps",
    "Associate Data Scientist",
    "Senior Frontend Engineer (Angular)",
    "Staff Engineer, Frontend - Domains Growth",
    "Senior Software Engineer I, Full-stack",
    "Infrastructure Engineer",
    "Staff Product Security Engineer",
    "Senior Engineer, Civil",
    "Data Management Analyst",
    "Lead Software Engineer",
    "Software Engineer, Frontend",
    "Senior Engineering Manager, Developer Productivity",
    "Senior Sales Engineer",
    "Staff Backend Software Engineer- (AI Platform)",
    "Senior Software Engineer, Platform",
    "Senior Infrastructure Engineer",
    "Senior Product Data Analyst",
    "Senior Software Engineer, Android",
    "Senior Value Engineer",
    "Domain Architect",
    "Principal Engineer - Privacy",
    "Staff Android Engineer (Clients Platform)",
    "Staff Machine Learning Engineer",
    "Backend Developer Junior",
    "Principal Engineer, iOS Performance",
    "Product Data Analyst",
    "Data Product Analyst, Corporate",
    "Senior AI Agent Engineer",
    "Software Engineer, Forward Deployed Agent Builder",
    "Senior Software Engineer, Data Infrastructure",
    "Principal Analytics Engineer",
    "Sales Engineer",
    "Senior Web Engineer",
    "Mobile Medical Assistant - Per Diem In-Home",
]

fixture = json.loads(FIXTURE.read_text())
tid = next(iter(fixture["targets"]))  # the FE target

seen: set[str] = set()
cases = []
for t in RAW_TITLES:
    t = t.strip()
    if t and t.lower() not in seen:
        seen.add(t.lower())
        cases.append({"target_id": tid, "title": t})

fixture["cases"] = cases
FIXTURE.write_text(json.dumps(fixture, indent=2))
print(f"Wrote {len(cases)} distinct hard-adjacent titles for target {tid}")
print(f"target label: {fixture['targets'][tid]['target']['label']}")
