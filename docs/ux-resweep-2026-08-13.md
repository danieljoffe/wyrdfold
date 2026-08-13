# Fresh-eyes re-sweep — 2026-08-13 (post-release verification)

A second full new-user walk of prod (wyrdfold.com), run immediately after release
`main@1aeb282e` (#716) deployed the 2026-08-12 sweep's eight PRs. Two purposes: verify
every shipped fix reads correctly **in situ on prod**, and hunt fresh issues with the
same fresh-eyes bar as `docs/ux-sweep-2026-08-12.md`.

Session side effects (owner account): one fit analysis on the Anduril "Staff Software
Engineer" posting (~$0.02; "Deep analyses today 2 of 20"); onboarding redone and
skipped (state preserved). No other mutations.

---

## Verified fixed on prod (all observed live)

| Surface          | Observation                                                                                                                                                                                                        |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Home             | Toggle reads **Overview / Trends**; tiles no longer imply daily counts                                                                                                                                             |
| Trends           | Weekly Activity axis now extends into the **current week** (was ending 10 days early)                                                                                                                              |
| Trends           | Almost Matched chips correctly cased — "Senior Associate, **IT** Internal Auditor", "**B2C** Customer Experience Associate I", "(**CD&AI**)", "**C/C++**"; the "\_field" artifact is gone; subtitle explains the % |
| Jobs (Score 85+) | The pending-only note renders in the exact repro state; statuses title-cased ("Resume Draft")                                                                                                                      |
| Job detail       | "**Fit analysis**" heading; "Generate tailored resume" / "Generate cover letter" consistent; analysis runs 202+poll and persists                                                                                   |
| Search           | "€54k–€75k" (was "EUR 54k–EUR 75k"); modal hint "unlock fit analysis and resume tailoring"; picker sorts **active targets first** and labels the rest "inactive"; menu self-scrolls inside the modal               |
| Targets          | "Add target / Suggest from experience / Suggest lateral roles" sentence-cased; scoring categories render "CORE SKILLS" (humanized + label-uppercased), not `CORE_SKILLS`                                           |
| Target detail    | Preferences footnote states tagging is rolling out; Learning tab says **WyrdFold** and uses the real button labels ("Great match" / "Not for me")                                                                  |
| Profile          | **Every Experience card now shows the stored month** (FightCamp "Jan 2021 – Jan 2023", was Dec–Dec); "4 evidence items" chips                                                                                      |
| Settings         | "isn't available on this server", "30-day allowance", hosted-keys BYOK note                                                                                                                                        |
| Onboarding       | Path C: "**Step 2 of 4**" (was "Step 3 of 5") and the question now opens with "Why this question — from your saved experience: …" including the API's word-boundary ellipsis (#712 × #713 composing correctly)     |

## Fresh findings (new this pass)

| #   | Finding                                                                                                                                                                                                                                                                                                                                                                                   | Severity / note                 |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------- |
| R1  | "Score Breakdown" (Title Case) sits directly beside "Fit analysis" (sentence case) on the job detail — the casing inconsistency the rename made visible                                                                                                                                                                                                                                   | copy nit                        |
| R2  | **Fresh LLM analyses still emit internal vocabulary**: this pass's Anduril analysis says "no evidence in **payload**", "not assessable from **payload**", third-person "the candidate". Expected — prompt text was deliberately excluded (spend-bearing evals) — but now confirmed to recur in NEW output, not just cached rows. The owner-call on prompt tightening is the only fix path | known-deferred, confirmed live  |
| R3  | Target cards' fit badges (91 / 14 / 62) remain unlabeled — only the detail page says "Fit 91". This was in the original report's prose but **never made it into the fix tables**, so it was never executed                                                                                                                                                                                | carried-over, add tooltip/label |
| R4  | The failed target card shows no failure date: the failure predates the `activation_failed_at` column, so the (shipped) timestamp has nothing to render. Code is right; the one stale card stays undated until a retry refreshes it                                                                                                                                                        | data gap, self-heals on retry   |
| R5  | Settings AI-usage line "your oldest spend rolls off around **8/13/2026**" — that's _today_, and with continuous usage the rolling-window date is perpetually ≈now. Consider hiding the line when the date ≈ today                                                                                                                                                                         | copy/logic nit                  |
| R6  | Add-to-target menu truncates long labels ("All Levels: Fullstack Software En…") with no tooltip                                                                                                                                                                                                                                                                                           | polish                          |
| R7  | Title junk persists as documented ("(1508) Senior Fullstack Engineer (Python, Node, C# & React)") — the deferred D4 ingest-design item, still visible in search                                                                                                                                                                                                                           | known-deferred                  |
| R8  | Perceived performance: several authed pages (Profile, Settings, target tabs) hydrate through multi-second skeletons on this pass's navigation. Observed right after a deploy (cold caches) and through the browser extension (whose timings are unreliable per standing note) — **flagging as an observation to measure properly, not a verdict**                                         | needs real measurement          |

## Verdict

The release holds up under a fresh-eyes walk: every shipped fix reads correctly in
place, the two cross-app compositions (onboarding context line, near-miss casing)
behave exactly as designed, and no regression was found. The remaining rough edges are
the four known-deferred items (R2 prompt vocabulary, R7 title junk, `title_display`,
shared-ui Dropdown portal) plus four new nits (R1, R3, R5, R6) small enough for a
single polish PR.
