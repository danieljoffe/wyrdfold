# `/targets` fresh-eyes UX sweep — 2026-08-14

A full walk of the `/targets` routes on prod (wyrdfold.com) role-playing a first-time user
with borrowed account access. Follow-up to `onboarding-sweep-2026-08-14.md`, which covered
the onboarding wizard; this one commits to the target workflows wherever they lead
(into `/jobs`, into all four target-detail tabs, and through all four creation paths).

Everything below was observed live. Findings marked **confirmed** were reproduced, and
several were confirmed at the API layer (response bodies quoted). Two initial conclusions
were **retracted** after checking — they are recorded at the bottom so they don't get
re-litigated.

Session side effects (owner account): four test targets were created — one per creation
path — and **all four were deleted**. The account is back to its original 10 targets, all
Inactive. No scoring spend was left running.

Methodology note: the automation tab runs with `document.visibilityState === "hidden"`,
which stalls React effects and parks pages on skeletons indefinitely. Visibility was
patched after each navigation. **No load-time or skeleton-duration observation is reported
here** — that environment cannot measure them (see `feedback_browser_mcp_background_throttling`).

---

## A. Flow stoppers — the user is blocked, or silently gets the wrong thing

### A1 — A 20–48s AI action reports "nothing found" only via a 4-second toast **confirmed, cause corrected**

Clicking "Suggest from experience" shows a ~20s `Suggesting…` spinner, then returns to
idle with the page looking exactly as it did before the click.

Confirmed at the API layer — the call succeeds and legitimately has no matches:

```
POST /api/targets/suggest  →  200  {"matches":[]}
```

> **Correction.** My first read was "the empty branch is unhandled". That is **wrong** and
> was retracted after reading the code. `TargetsList.tsx:466-473` _does_ handle it, with a
> good message ("No new suggestions — Your existing targets already cover roles that fit
> your experience"), and `:530-537` does the same for lateral. I missed it because toasts
> auto-dismiss after **4000ms** (`ToastProvider.tsx:56`) and I screenshotted ~24s after
> clicking.

The real defect is narrower but still real: **for an operation that takes 20–48 seconds,
a 4-second toast is the only feedback, and it leaves no trace in the page.** Both results
grids are hard-gated on `length > 0` (`:688`, `:736`), so an empty result adds nothing to
the DOM. A user who looks away during the long wait — the overwhelmingly likely behaviour —
returns to a page that cannot tell them whether anything happened.

The product already has the right two-layer pattern one tab away: `LearningLogPanel.tsx`
pairs the transient toast (`:276-284`) with **durable in-page copy** (`:339-344`). The
suggest buttons have only the first layer.

Secondary: the zero-state variant of the button (`TargetsList.tsx:600-609`) has no spinner
and no label change at all — it only goes `disabled`, so a first-time user with no targets
gets the weakest feedback of anyone during the same long call.

### A2 — "View jobs" on an **inactive** target silently shows a different target's jobs **confirmed**

Choosing "View jobs" on an inactive target lands you on bare `/jobs` — showing whichever
_other_ target happens to be active, with no explanation.

> **Mechanism corrected.** I first assumed the card built the URL conditionally. It does
> not: `TargetsList.tsx:243-248` pushes `/jobs?target=${id}` **unconditionally**. The
> parameter is stripped server-side by `jobs/page.tsx:42-51`, which redirects to `/jobs`
> when the id isn't in the tab list — and `toActiveTargetTabs` (`jobs/targetTabs.ts:14-20`)
> builds that list from `user_target.is_active` only. So an inactive target is absent, and
> the guard that exists to avoid rendering an empty list silently discards the user's
> intent instead. The fix belongs on the jobs page, not the card.

This is a known trap, not a hidden one: `TargetCard.tsx:18-30` documents it in a comment,
and `__tests__/TargetCard.spec.tsx:222` deliberately asserts "View jobs" stays _enabled_
on a deactivated target — i.e. the card knowingly offers a link the jobs page discards.

Reproduced twice at the UI level, with different consequences depending on account state:

- **With another target active:** clicking "View jobs" on the inactive _Senior Full Stack
  Engineer_ landed on `/jobs?score=85&min_salary=200000&country=US` with the **All Jobs**
  tab selected, showing _Product Manager_ postings. There is no tab for the target that
  was clicked, and nothing explains the substitution.
- **With no targets active:** landed on the empty state _"No active targets. Activate a
  target to start seeing matched jobs."_ — which never names the target that was clicked,
  and whose "Go to Targets" button returns the user exactly where they started.

Compounding cause: `/jobs` only builds per-target tabs for **active** targets, so an
inactive target's jobs are unreachable by any route.

### A3 — Three of four creation paths land the target Inactive, with no prompt **confirmed**

| Path                              | Endpoint                        | Resulting state |
| --------------------------------- | ------------------------------- | --------------- |
| Search tab → **Follow**           | (follow/link)                   | **Active**      |
| Manual tab                        | `POST /api/targets/from-manual` | Inactive        |
| Lateral suggestion → "Add target" | `POST /api/targets/from-manual` | Inactive        |
| From URL tab                      | `POST /api/targets/from-url`    | Inactive        |

Nothing in the UI states which happened, or that Inactive means no jobs will ever be
matched. The likeliest first action for a new user — **Add target → Manual** — produces a
card that quietly does nothing, and the obvious next step ("View jobs") walks straight
into A2.

Decision taken: **keep creation inactive and prompt**, rather than auto-activating.
Auto-activation would silently start LLM scoring spend on every creation.

### A4 — Target names are taken verbatim from the source, and can never be changed **confirmed**

The From URL modal warns "the title comes from the posting itself", and it means it. The
test URL produced a target literally named:

> `Senior Product Builder (Product Manager), Enterprise Readiness & Admin Platform`

78 characters, company-specific, truncated on the card. There is **no rename or edit
anywhere** — the kebab offers only View jobs / Activate / Delete, and the detail page has
no identity controls. The only escape is delete-and-recreate, which discards the reference
JDs and learning log that make a target worth keeping.

Decision taken: **do not add user-editable rename.** Target names should stay uniform and
role-shaped rather than job-specific, so the fix is to **normalize the name at creation
time** — either extracted cleanly from the source or produced by the LLM as a canonical
role-profile name. This also improves catalog dedup, since a normalized name is far more
likely to collide with (and reuse) an existing catalog entry.

### A5 — A failed "From URL" discards the input and recommends an impossible action **confirmed**

Submitting a non-job URL returns a well-worded error toast:

> "Could not extract a job description from that URL. Try pasting the JD text directly."

But the modal closes, the typed URL is gone, and **there is no JD-paste field anywhere in
the creation flow**. Pasting a JD is only possible on the Reference JDs tab of a target
that already exists — which the user does not have, because creation just failed.

```
POST /api/targets/from-url  →  422
```

---

## B. Confusing or risky

### B1 — The delete confirmation never names the target **confirmed**

The dialog reads, in full:

> **Delete target?**
> Saved jobs scored against this target lose their target context. This cannot be undone.
> `Cancel` `Delete`

The account carries six near-identical names — _Senior Full Stack Engineer_, _Senior
Full-Stack Engineer_, _Staff Full-Stack Engineer_, _Senior Frontend Engineer_, _Senior
Frontend Engineer – Web Performance_, _Staff Frontend Engineer_. Nothing lets the user
verify which one is about to be permanently destroyed, and there is no undo (correctly, per
the warning — which is exactly why the name matters).

### B2 — Inactive targets show no status on the detail page **confirmed**

The detail header renders an `Active` pill only when the target is active. When inactive it
renders **nothing at all** — not "Inactive". Combined with the fact that the detail page has
no activate control, a user can tune weight axes and preferences at length on a target that
is switched off and never learn it.

### B3 — "Minimum fit score" accepts up to 200 on a 0–100 scale **confirmed**

The input carries `min="0" max="200"`. Saving `150` succeeded cleanly: the panel badge
flipped `Defaults` → `Custom` and the toast said "Preferences saved". The notification
threshold inputs on the same page correctly use `max="100"`.

Once grading catches up this hides every job for that target, with nothing in the UI
explaining why. The accept-and-save was confirmed; the resulting empty list was **not**
observable in-session because that target's jobs were all still ungraded and the page
deliberately keeps ungraded jobs visible.

### B4 — The fit-score explanation is trapped in a native `title` tooltip **confirmed**

Each fit badge carries a ~600-character AI explanation naming exactly which skills are
missing — the single most useful piece of content encountered in the sweep. Example
(Product Manager, fit 22), abridged:

> "…the target's highest-weighted core skills — PRD writing, formal user research,
> stakeholder management, prioritization frameworks, and product roadmap ownership —
> appear nowhere in the profile's listed skills…"

It lives only in a native `title` attribute: unreachable on touch, unreachable by keyboard,
~1s hover delay, and truncated by some browsers. On the index the badge is a bare colored
number with no label at all.

Vocabulary also drifts across five surfaces for one number: `aria-label` says **"Match
score 22"**, the `title` says **"Fit score 22"**, the detail header says **"Fit 42"**,
preferences say **"Minimum fit score"**, and the jobs URL uses **`score=`**. The
`title`/`aria-label` disagreement is a single line — `ScoreBadge.tsx:68-69` — and the same
component's _pending_ branch already says "Fit score pending", so it contradicts itself.

Worse on the detail page: that header's "Fit NN" chip is a plain `Badge`
(`TargetDetail.tsx:311-316`), **not** `ScoreBadge` — so it carries no `title`, no
`aria-label`, and no score→colour mapping. The reasoning text is not merely hard to reach
there; it is absent.

### B5 — One target is permanently stuck with no fit score **confirmed**

_Senior Full Stack Engineer_ renders no badge on its card and no `Fit` chip on its detail
page, while all ten others do. There is no recompute or retry affordance anywhere, so the
record is stuck indefinitely.

Note: this is an individually stuck record, **not** a path-wide bug — see R3 below.

### B6 — "Updated" dates render in UTC **confirmed, root cause found**

A target created at **17:11 PDT on 2026-08-14** displayed `Updated 8/15/2026`. Every target
updated earlier the same day showed `8/14/2026`. Anything touched after ~17:00 Pacific
appears dated a day ahead. Same class as A1/A2 in the 2026-08-12 sweep.

Root cause is `LocalDate` in `components/LocalFormat.tsx:52-64`. The intent (documented at
`:1-34`) is viewer-local formatting, but the span carries `suppressHydrationWarning`, which
tells React to skip both the warning **and the text correction**. The server renders the
date in UTC, hydration leaves that server text in place, and nothing re-renders the span
afterwards — so a Pacific viewer keeps seeing UTC's tomorrow all evening.

So the fix is a mount-gated re-render (or `useSyncExternalStore`), **not** a formatting
change. Note `__tests__/LocalFormat.spec.tsx:34-35` currently asserts
`suppressHydrationWarning === true`, so it pins the buggy contract and must change with the
fix. Every `LocalDate` caller shares the bug — at minimum `TargetCard.tsx:160`,
`LearningLogPanel.tsx:185`, `ReferenceJDList.tsx:158`.

---

## C. Smaller things

| #   | Finding                                                                                                                                                                                                                                                                   |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| C1  | After a **successful** Follow, the only button in the modal is **"Cancel"** — no "Done"/"Close", and no success toast. It reads as though it undid what was just done. (By contrast, a duplicate manual create closes the modal and toasts "Already in your targets: …".) |
| C2  | The lateral-roles results panel cannot be dismissed; it persists until a page reload. It also takes ~48s with only a "Suggesting…" label and no cancel.                                                                                                                   |
| C3  | An unknown `?tab=` value silently falls back to Scoring. The Reference JDs slug is `jds`, not `reference-jds`, so a mistyped or stale shared link lands on the wrong tab with no indication.                                                                              |
| C4  | Target cards are `div[role="button"]`, not `<a href>` — no cmd/middle-click to open in a new tab, and no link to copy. (Accessibility is otherwise correct: proper `aria-label` and `tabindex="0"`.)                                                                      |
| C5  | The detail page fires **6–8 identical** `GET /api/targets/{id}/user-target` requests per visit.                                                                                                                                                                           |
| C6  | Fit-score drift: _Founding Engineer_ read **95** for the first stretch of the session and **82** afterwards, untouched, and stayed 82 across reloads. Trigger could not be determined from the UI. Needs a separate look.                                                 |

---

## D. Verified working — do not "fix" these

- **Duplicate handling.** Manually creating "Founding Engineer" when already following it
  toasts _"Already in your targets: Founding Engineer"_ and creates nothing.
- **Activate / Deactivate.** Instant, correct toggle label, clear toast.
- **Unknown target id.** `/targets/<bogus-uuid>` renders "Target not found" with a
  "Back to targets" action.
- **The ungraded-jobs explainer** on `/jobs` is genuinely clarifying.
- **Escape closes modals**; tab state is reflected in the URL.
- **Search → Follow** is the cleanest flow on the page.
- **Lateral-role suggestion quality** is high — match %, domain tag, and a specific,
  evidence-citing rationale per card.

---

## E. Retracted mid-sweep — do not resurrect

| #   | Initial claim                                                               | What checking showed                                                                                                                                  |
| --- | --------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| R1  | "Card click areas are inconsistent — only the header navigates."            | **Wrong.** The `div[role="button"]` covers 173px of the 174px card. The failures were automation clicking before hydration completed.                 |
| R2  | "'View jobs' never passes a target."                                        | **Wrong.** It passes `?target=<id>` correctly — but only when the target is active. Narrowed into A2.                                                 |
| R3  | "The From URL path skips fit scoring."                                      | **Wrong.** The From-URL test target did receive a fit score (18); it just takes ~40s longer than the other paths. B5 is an individually stuck record. |
| R4  | "The last lateral-suggestion card renders without its 'Add target' button." | **Wrong.** All 8 buttons were present in the DOM; the last was simply below the screenshot fold.                                                      |
