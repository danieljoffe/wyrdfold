# /jobs flow-stopper sweep — 2026-08-15

New-user exploration of the `/jobs` routes on **wyrdfold.com** (prod), same playbook as
the onboarding sweep (`docs/onboarding-sweep-2026-08-14.md`) and the two `/targets`
sweeps. Goal: find places where a user is **left without recourse**, **left confused**,
or **cannot finish a task they intended**.

Account: `hello@danieljoffe.com`. Three targets were activated for the sweep
(Senior Full Stack Engineer, Senior Frontend Engineer, Frontend UX Designer) and
**all three were deactivated again at the end** — the account is back to 0 active
targets. Test writes are listed in §4.

Surfaces covered: `/jobs` list (filters, search, sort, target tabs, pagination, batch
bar), inline detail panel, `/jobs/[id]`, `/jobs/[id]/resume`, `/jobs/[id]/cover-letter`,
match analysis, ATS recheck, version history, delete, and add-job-by-URL.

---

## 1. Flow stoppers — ranked

### P0-1 — Add-a-job-by-URL reports success when nothing was added

`POST /api/jobs/manual` returns **HTTP 200** with a `success: false` body when the
extractor cannot read the page:

```json
{
  "success": false,
  "posting_id": null,
  "extraction_tier": "none",
  "needs_manual_fields": true,
  "warnings": ["http_status:404", "firecrawl_failed:http_403", "fetch_non_200"]
}
```

`useAddJobByUrl.ts:30` only branches on `if (!res.ok)`. A 200 is `ok`, so the hook takes
the success path and shows a green **"Job added"** toast, then refetches a list that has
not changed.

Verified live on prod:

| URL                                    | HTTP | body                         | user sees            |
| -------------------------------------- | ---- | ---------------------------- | -------------------- |
| `not a url`                            | 400  | `{"detail":"Malformed URL"}` | correct error        |
| `https://example.com/some-page`        | 200  | `success:false`              | ✅ "Job added" (lie) |
| `https://www.linkedin.com/jobs/view/…` | 200  | `success:false`              | ✅ "Job added" (lie) |

LinkedIn is the most likely place a user copies a job URL from, and it 403s the
extractor. The user is told it worked, sees no new job, and has no error to act on.
The backend even sets `needs_manual_fields: true`, implying a "fill in the details
yourself" follow-up that the frontend never implements.

**Fix:** branch on the body, not just `res.ok`. On `success: false` surface the reason
and either offer manual entry (the API is already asking for it) or say plainly that the
site can't be read and to paste the ATS URL instead.

### P0-2 — Resume version history can never restore anything

Repro: generate a tailored resume → edit the markdown → Version history → **Load**.
Result: red toast **"This version predates markdown — cannot restore"**, content
unchanged. It fires for the `initial` version created _minutes_ earlier, so the copy is
also factually wrong.

Root cause, proven against prod `GET /api/jobs/tailor/{id}/versions`: version rows
serialize `id, resume_id, payload, source, created_at`. **There is no `payload_md` key
at all.** `ResumeReviewPage.tsx:480` reads `version.payload_md`, gets `undefined`, and
bails every time. Restore is therefore broken for 100% of versions, not an edge case.

**Correction (added after source review).** I originally wrote this up as a second,
deeper defect — "versions snapshot the structured payload, so markdown edits are never
captured". That is **wrong**, and the evidence that led me there was an artifact of the
same bug. `versions.checkpoint()` _does_ write the edited markdown to
`document_versions.payload_md`, and the column has existed since the base schema. What I
observed over the API (`payload` byte-identical between `initial` and `user_edit`, my
edit absent) is exactly what you'd see when the markdown is stored but stripped on the
way out.

The real root cause is a single serialization bug: `ResumeVersion` in
`app/services/tailor/versions.py` has no `payload_md` field and sets
`model_config = {"extra": "ignore"}`. `list_for_resume` does `select("*")` — so the DB
returns the markdown and **Pydantic silently drops it** before
`model_dump()`. One missing field breaks restore for every version.

One genuine secondary gap remains: `persistence.update_payload` (`persistence.py:284`)
calls `versions.record(...)` **without** `payload_md`, so snapshots taken on that path
really do lack markdown. That is the case the "predates markdown" guard was written for.

**No migration required** — `document_versions.payload_md` already exists.

This is the only undo affordance for a hand-edited resume, and the "Re-adapt with AI"
dialog explicitly promises **"Current draft is saved as a version first"** at the moment
of a destructive action. That safety net does not exist. "Free tier keeps the last 5
versions" advertises it further.

**Fix:** include the markdown in the versions payload and snapshot the markdown (not the
structured payload) on user edits. Until then the "Load" buttons and the reassurance
copy should not be shown.

### P1-3 — "Delete" is an archive, claims to be irreversible, and leaves the row in the list

Batch Delete → modal **"Delete 2 jobs? This can't be undone."** → toast **"Deleted 2
jobs"**. Actual behaviour: `DELETE /api/jobs/{id}` soft-deletes to `status='archived'`.
Verified via the default unfiltered list query — both jobs still returned
(`{archived: 2, new: 97, resume_draft: 1}`) and still rendered, just relabelled
"Archived".

**Sharpened after source review.** The soft delete is deliberate and correct: the DELETE
handler's docstring records that a hard delete would wipe `status_log` / `user_jobs` for
_every other user_ following the same posting — cross-tenant destruction, closed out by a
prior security audit (#29 round 3 / H1). It also states archived rows are ones "the
list/counts endpoints already filter out".

That last claim is only half true, and **that** is the actual bug. The Python hydration
path does drop them (`rows = [r for r in rows if r["status"] != "archived"]`,
`jobs.py:365`), but both SQL RPCs — `get_target_jobs` and `get_cross_target_jobs` —
gate status with:

```sql
AND ($4 IS NULL OR COALESCE(uj.status, 'new') = $4)
```

With no status filter (the default view) `$4` is NULL and **no archived exclusion is
applied at all**. A default `sort=score` list routes through the cross-target RPC, which
is exactly the path I hit. So archived leaks on the RPC paths and is filtered on the
Python path — the two disagree.

1. The user's actual intent — get this out of my list — is not served.
2. The copy claims irreversibility, but `archived` is one of the nine normal statuses.
   I reversed it with `POST /api/jobs/{id}/status` → `{"old_status":"archived",
"new_status":"new"}`. Users are being scared off a safe action.
3. The status filter is **single-select**, so there is no "everything except Archived" —
   archived rows pollute the default view permanently with no way to hide them.
4. No Undo in the toast, despite undo being trivial for a soft delete.

**Fix:** pick one and make the UI honest. Either exclude `archived` from the default
list and relabel the action "Archive" with an Undo toast, or make Delete actually
delete. The current copy/behaviour mismatch is the worst of both.

### P1-4 — Add-by-URL disappears once you have 5 or more jobs

`useAddJobByUrl` is only mounted by `JobsEmptyState` (0 jobs) and
`JobsThinResultsCallout` (1–4 jobs). On a healthy list there is **no entry point
anywhere in `/jobs`** to add a posting you found yourself — confirmed live, zero add
affordances on a populated list. "I found a job elsewhere and want to tailor a resume
for it" is a core job-to-be-done that becomes unreachable exactly when the product is
working well.

### P1-5 — A paid cover letter refuses to apply, with no warning and no override

On a job whose own match analysis says **"Skip"** (match 46), "Generate cover letter"
charged **$0.0450** and produced a letter that declines on the user's behalf:

> "…I am a full-stack engineer, not a UX designer. I am not applying for this role. I am
> flagging this mismatch so the record is clear rather than generating a misleading cover
> letter for a position I am not qualified for."

The honesty is good; the flow around it is not. There is no warning **before** spending
that the analysis already said Skip, no "write it anyway / lean on transferable skills"
override, and no retry that isn't another paid generation. Neither document button has
any pre-spend confirmation.

---

## 2. Confusion / discoverability (lower severity)

| #   | Finding                                                                                                                                                                                                                                                                                                                                                                                    |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 6   | **Row titles link off-site.** Every title is `<a href="{ATS}" target="_blank">`; there are **zero** internal `/jobs/{id}` links in the list. The internal detail page — which holds the JD, score breakdown, match analysis and tailoring — is reachable only via the small ⤢ icon inside an expanded row.                                                                                 |
| 7   | **Per-target tabs show unrelated ungraded intake.** The "Frontend UX Designer" tab listed "Embedded Software Engineer – Power Electronics" (Redwood Materials), "Senior AI/ML Engineer – Vaccine R&D" (Pfizer), "Systems Engineer I – Tac Comm". Ungraded jobs bypass the target filter, so a named tab reads as a firehose.                                                               |
| 8   | **"Score 85+" returns only ungraded jobs.** Every visible row was `·`. The callout explains the escape hatch well, but the user's intent is unserved and there's no "graded ≥85 only".                                                                                                                                                                                                     |
| 9   | **"Re-adapt with AI" leaves the old draft on screen.** Regenerate mints a new tailor record id; the page kept rendering the previous markdown _and_ an empty version-history panel until a manual reload. A user concludes it failed and pays again.                                                                                                                                       |
| 10  | **No bulk status change.** The batch bar offers only "Generate tailored resumes" (N × spend, no cost preview or confirmation) and "Delete". Bulk triage — mark Saved / Not for me / Rejected — is missing.                                                                                                                                                                                 |
| 11  | ~~**No save state on the resume editor.**~~ **RETRACTED after source review.** There _is_ an indicator — `saveLabel(saveStatus)` renders "Editing — autosave pending" / "Saving…" / "Saved" under the editor. It is empty only at rest (`idle`), which is why I read it as absent. What stands: edits autosave correctly, and there is still no explicit "done / applied" terminal action. |
| 12  | **ATS recheck result is toast-only.** `POST …/ats-recheck` returns 200 and reports via an auto-dismissing toast; a passing resume has no persistent "ATS: passing" state.                                                                                                                                                                                                                  |
| 13  | **Native `window.prompt` for critical input** — add-by-URL (`useAddJobByUrl.ts:24`), missing contact name (`promptForMissingContactName.ts:37`), editor link (`MarkdownPreviewEditor.tsx:159`). No validation, no inline error, unstyleable, and it blocks the page.                                                                                                                       |
| 14  | **Copy inconsistency**: "Review tailored resume" next to "Review Cover Letter". Job status also stays "Resume Draft" after a cover letter exists.                                                                                                                                                                                                                                          |

---

## 3. What works well

- **Background tailoring survives navigation.** Left mid-generation, came back, draft was
  there and status had flipped to "Resume Draft" (202 + poll, #656).
- **Resume edits autosave reliably** across full page navigations.
- **Match analysis is honest and specific** — "Skip" plus seven concrete missing skills;
  the generated resume did _not_ fabricate design experience for a UX role.
- **Cost transparency**: "Generated for $0.0679" on the review page.
- **Empty state has real recourse**: per-chip ×, "Clear all", and an add-by-URL escape.
- **Filter architecture is a genuine single source of truth** (`jobsFilterFields.ts`) —
  URL, persistence, and API params all derive from one list, with compile-time pins.
- **Delete uses a proper in-app ConfirmModal**, not `window.confirm`.

---

## 4. Test writes made against the prod account

- Activated then **deactivated** 3 targets (net zero; account back to 0 active).
- Job `d08abe33…` (Senior UX Designer @ SecurityScorecard): ran match analysis,
  generated a tailored resume, edited it, ran an ATS recheck, re-adapted with AI, and
  generated a cover letter. **Two tailor records and one cover letter remain** on this
  job; status is "Resume Draft". Not reverted — deleting them wasn't offered in-product.
- Batch-deleted 2 jobs ("Senior Visual Designer, Games", "Senior Product Designer") and
  **restored both to `new`** via the status endpoint.
- 4 `POST /api/jobs/manual` probes with invalid/unreadable URLs — all failed extraction,
  so **no postings were created**.
- One stray edit to the resume markdown was overwritten by the Re-adapt regeneration;
  the final resume is clean (contact line and content verified correct).

## 5. Method notes

- Perf/latency observations are **not** reported: the automation tab ran with
  `visibilityState: hidden`, which throttles React effects by 15–30s. Skeleton "hangs"
  seen during the sweep were that artifact, not the app.
- Two suspected bugs were **retracted after verification**: menu items that "did
  nothing" were coordinate misses (ref-clicks fire correctly), and row checkboxes that
  "expanded instead of selecting" are a 1×1 hidden input with a styled 20×20 span —
  clicking the visible control works correctly for a real user.

---

## 6. Shipped — released and verified in prod (2026-08-16)

Released as `main@8b2e621a` (PR #783 = #777, #778, #779, #781, #782). Migration
`20260816010000_user_target_job_removals` applied to prod **before** the merge, verified,
and stamped. Frontend deployed with `vercel --prod`; Railway auto-deployed the API.

**Deploy discriminators** (captured pre-deploy, re-checked after):

| Probe                                 | Before          | After                                       |
| ------------------------------------- | --------------- | ------------------------------------------- |
| `POST /jobs/{id}/remove` (unauth)     | route absent    | **401** (control: unknown route still 404s) |
| `/api/jobs/tailor/{id}/versions` keys | no `payload_md` | **`payload_md` present, 2,820 chars**       |

**Verified in prod:**

| #     | Claim                                  | Result                                                                                                                                                                               |
| ----- | -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| P0-1  | LinkedIn URL no longer reports success | ✅ In-app dialog shows _"This site blocks automated readers… paste the employer's own application link"_, offers the manual title/company/location fallback, and keeps the typed URL |
| P0-2  | Version restore works                  | ✅ Load → confirm → restored, content intact (2,820 chars), no "predates markdown" error                                                                                             |
| P1-3  | Remove is per-target and reversible    | ✅ Removed job gone from the score sort, the two-query title sort **and** the cross-target RPC; Undo restored it; shared job row + score (72) untouched                              |
| P1-3b | Archived no longer leaks               | ✅ Excluded from the default view, still visible under `status=archived`. Both RPCs confirmed directly in prod                                                                       |
| P1-4  | Add-by-URL reachable at any list size  | ✅ "Add job by URL" in the toolbar on a populated list                                                                                                                               |
| P2-6  | Titles open the job in-app             | ✅ 0/5 title cells are outbound anchors (was all of them); every row has a labelled apply icon; clicking a title expands the row; internal `/jobs/` links exist again                |
| —     | Confirm copy                           | ✅ _"Remove 20 jobs from Frontend UX Designer? They will stop appearing here. You can undo this."_ — names the target, no false irreversibility                                      |
| —     | Regressions                            | ✅ No console errors on fresh loads                                                                                                                                                  |

**One false alarm worth recording**: an archived job appeared to still leak post-deploy.
It did not — `job_list_cache` has a **60 s TTL** and the re-read landed inside the window
my own "before" call had populated. Both RPCs, queried directly against prod, excluded it
correctly. Re-tested past the TTL: excluded. _A cache is not a bug; check the TTL before
declaring a regression._

**Not shipped:**

- **#780 (draft)** — the cover-letter _"write it anyway"_ prompt override. Code, prompt,
  golden re-baseline and a new eval (`scripts/eval_cover_letter_stretch.py`) are all done,
  but CONTRIBUTING requires the eval to actually run before a prompt change merges, and it
  can't unattended: the `~/.zshrc` `OPENROUTER_API_KEY` returns `401 User not found` and
  Railway auth is expired. One command unblocks it — see the PR.
- P2 #7, #8, #9, #10, #12 — deliberately deferred, listed in #782.

**Account restored**: 0 active targets, 0 leftover removal rows, the verification removal
undone.
