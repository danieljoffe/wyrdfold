"""Hourly health check on catalog admission.

Run: `railway run -- uv run python scripts/admission_watch.py [hours]`
from apps/wyrdfold-api (needs PYTHONPATH=. so the poller gate imports).

Reports the numbers that matter after the #952/#954 admission work:
  * intake volume vs the hourly ceiling
  * the TITLE MIX -- the thing the #952 regression was invisible to, because
    the pre-deploy measurement counted how many listings passed the gate and
    never looked at what they were
  * frontend arrivals, which is what the widening was for
  * whether the live gate still agrees with what actually got ingested
"""

import os
import re
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/apps/wyrdfold-api")
from supabase import create_client  # noqa: E402

from app.services.poller import _admits_for_catalog  # noqa: E402
from app.services.targets import crud  # noqa: E402

HOURS = int(sys.argv[1]) if len(sys.argv) > 1 else 1
JUNK = re.compile(
    r"\b(assistant|associate|representative|coordinator|specialist|nurse|"
    r"cashier|server|teller|receptionist|clerk|instructor)\b",
    re.I,
)
ENG = re.compile(r"engineer|developer|scientist|designer|product manager", re.I)

# "Frontend" is ambiguous across two unrelated industries. In CPU design the
# FRONT END is the instruction fetch/decode stage, so "Senior RTL Design
# Engineer - CPU Frontend" matches a naive regex and silently inflates the very
# number we use to judge whether #952 worked. Observed 2/2 arrivals in one hour.
_FE_ANY = re.compile(r"front.?end", re.I)
_FE_SILICON = re.compile(
    r"\b(rtl|cpu|gpu|asic|soc|silicon|semiconductor|verilog|vhdl|"
    r"physical design|analog|microarchitect\w*|dft|pcb)\b",
    re.I,
)


def is_web_frontend(title: str) -> bool:
    """Frontend as in browsers, not as in instruction pipelines."""
    return bool(_FE_ANY.search(title or "")) and not _FE_SILICON.search(title or "")

sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
now = datetime.now(UTC)
since = (now - timedelta(hours=HOURS)).isoformat()


def count(**f):
    q = sb.table("jobs").select("id", count="exact", head=True)
    for k, v in f.items():
        if k == "live":
            q = q.is_("archived_at", "null").is_("purged_at", "null")
    return q


live = (
    sb.table("jobs")
    .select("id", count="exact", head=True)
    .is_("archived_at", "null")
    .is_("purged_at", "null")
    .execute()
    .count
)
_fe_rows = (
    sb.table("jobs")
    .select("title")
    .is_("archived_at", "null")
    .is_("purged_at", "null")
    .or_("title.ilike.*frontend*,title.ilike.*front-end*,title.ilike.*front end*")
    .limit(1000)
    .execute()
    .data
    or []
)
# Filtered in Python, not SQL: the silicon exclusion is a negative match the
# PostgREST ``or_`` filter cannot express cleanly.
fe_live = sum(1 for r in _fe_rows if is_web_frontend(r.get("title") or ""))
fe_silicon = len(_fe_rows) - fe_live

rows, page, PAGE = [], 0, 1000
while True:
    r = (
        sb.table("jobs")
        .select("title")
        .gte("cataloged_at", since)
        .order("id")
        .range(page * PAGE, page * PAGE + PAGE - 1)
        .execute()
        .data
        or []
    )
    rows += r
    if len(r) < PAGE:
        break
    page += 1
    if page > 20:
        break

n = len(rows)
print(f"ADMISSION WATCH  {now.isoformat()[:19]}Z   (last {HOURS}h)")
print(f"  live catalogue    : {live}")
print(f"  frontend live     : {fe_live}   (+{fe_silicon} silicon/CPU \"frontend\", excluded)")
print(f"  ingested          : {n}   (ceiling is 2000/h)")

if not n:
    print("  -- no intake in the window; check the poller is ticking --")
    raise SystemExit(0)

junk = sum(1 for r in rows if JUNK.search(r.get("title") or ""))
eng = sum(1 for r in rows if ENG.search(r.get("title") or ""))
fe = [r for r in rows if is_web_frontend(r.get("title") or "")]
print(f"  junk-shaped       : {junk}  ({100 * junk / n:.0f}%)   [was 60% during the bug, ~6% after]")
print(f"  engineering-shaped: {eng}  ({100 * eng / n:.0f}%)")
print(f"  frontend arrivals : {len(fe)}")
for r in fe[:5]:
    print(f"      {str(r.get('title'))[:60]}")

w = Counter()
for r in rows:
    for t in re.findall(r"[a-z]+", (r.get("title") or "").lower()):
        if len(t) > 3:
            w[t] += 1
print(f"  top title words   : {dict(w.most_common(8))}")

# Does the live gate still agree with what got in? A drift here means the
# deployed rules and the checked-out rules have diverged.
trows = sb.table("targets").select("*").limit(1000).execute().data or []
allt, act = [], []
for r in trows:
    try:
        t = crud._parse_target(r)
    except Exception:
        continue
    allt.append(t)
    if r.get("app_active"):
        act.append(t)
disagree = [r for r in rows if not _admits_for_catalog(r.get("title") or "", act, allt)]
print(f"  gate disagreement : {len(disagree)}  (rows ingested that this code would refuse)")
if disagree:
    print("      ^ non-zero means deployed rules != checked-out rules; investigate")

# Which ceiling is actually binding? This is what hid for a day: the admission
# ramp bound every cycle while the documented hourly ceiling sat at ~5%, and
# nothing reported it, so intake looked "fine" at a fifth of real supply.
from app.config import settings  # noqa: E402

cycles = 60 / settings.poll_tick_minutes
ramp_h = settings.persistent_block_admission_cap_per_cycle * cycles
print("\n  ceilings:")
print(f"    admission ramp  : {settings.persistent_block_admission_cap_per_cycle}/cycle  (~{ramp_h:.0f}/h)")
print(f"    hourly intake   : {settings.intake_max_new_jobs_per_hour}/h")
binding = "RAMP" if ramp_h < settings.intake_max_new_jobs_per_hour else "hourly cap"
head = n / max(HOURS, 1)
print(f"    tighter of the two: {binding}   |  actual intake {head:.0f}/h")
if head >= 0.8 * min(ramp_h, settings.intake_max_new_jobs_per_hour):
    print("      ^^ intake is AT the binding ceiling — supply is being deferred, raise it")
