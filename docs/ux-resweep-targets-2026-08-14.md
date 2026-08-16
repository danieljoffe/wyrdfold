# `/targets` second sweep — 2026-08-14

Follow-up to `ux-sweep-targets-2026-08-14.md`, run after that sweep's six PRs
(#742–#747) shipped in release `main@edcd22c9`. Same posture: a new user with
borrowed access, learning the app by using it. Live prod, owner account
(`hello@danieljoffe.com`), mutations allowed.

**Account state at sign-in:** 10 targets, **every one of them Inactive** — so
nothing was being matched at all. That framed the whole sweep.

**Prod state on exit:** restored. The two targets I created were deleted, the
two I activated were deactivated. Back to 10 targets, all Inactive, nothing
accruing scoring spend.

---

## 0. First: what the last sweep fixed, re-verified live

Checked before hunting for anything new, so this report does not re-litigate
shipped work.

| Fix                                                  | Verdict                                                                                                         |
| ---------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| #743 — Inactive state legible on the detail page     | ✅ Header renders an **Inactive** chip and an **Activate** button.                                              |
| #745 — posting titles canonicalized into role labels | ✅ `sr. backend engineer (remote) - URGENT HIRING!!` → **"Senior Backend Engineer"**. Clean.                    |
| #746 — caller-specific extraction error              | ✅ API returns "…or create the target manually and add this posting from its Reference JDs tab." Exactly right. |
| #747 — delete prompt names the target                | ✅ **Delete "Senior Backend Engineer"?**                                                                        |
| #742 — durable empty panel for suggest actions       | ✅ `SuggestEmptyPanel` present for both actions.                                                                |

Also verified working, not previously recorded:

- **Reference-JD merge is reversible.** Adding a JD mutates the shared profile
  (25 → 45 keywords); deleting that JD re-merges it back to exactly 25, with
  every trace of the added JD gone. The confirm dialog's promise ("the scoring
  profile will be re-merged") is true. **This is real recourse — do not report
  the merge as irreversible.**
- **Min/max seniority inversion is blocked** with an inline
  "Min seniority must not rank above max seniority", and Save is refused.
- **`/jobs` and Home both handle the zero-active-target case well**, each with
  a sentence and a button to `/targets`.

---

## A. Flow stoppers

### A1 — Every date on `/targets` shows **tomorrow** for ~7 hours a day **confirmed, with proof**

Measured live, mid-sweep:

```
browser local   Fri Aug 14 2026 23:37:10 GMT-0700 (America/Los_Angeles)
UTC             2026-08-15T06:37:10Z
card renders    "Updated 8/15/2026"        <- tomorrow, to the user
```

Not a cosmetic drift — a date in the **future**, on every card, for every user
west of UTC, every evening between ~17:00 local and midnight.

**Root cause, confirmed in code.** `LocalFormat.tsx` renders
`toLocaleDateString()` inside a `<span suppressHydrationWarning>`. That
attribute does what it says: React stops _warning_ about the mismatch, and
also stops _correcting_ it — the server's UTC text is kept in the DOM and
never replaced, because nothing re-renders the subtree after hydration. The
component's own doc comment argues against pinning to UTC on the grounds that
it "would silence React by showing US users the wrong day every evening";
`suppressHydrationWarning` produces precisely that outcome anyway.

Affects every `LocalDate` / `LocalDateTime` / `LocalNumber` caller app-wide,
not just `/targets`.

### A2 — Reference-JD merge produces case-variant duplicate keywords **confirmed, root cause found**

Pasted one fintech JD into "Senior Full Stack Engineer" → Reference JDs. The
shared scoring model came back with, inside a single category:

```
CORE SKILLS      Microservices  3       <- pre-existing
                 microservices  2       <- added by the merge
```

Same concept, two entries, two weights, one category — so the concept is
double-counted at 1.67× its intended weight whenever a JD mentions it.

**Root cause: `apps/wyrdfold-api/app/services/targets/merge.py`.**
`_merge_categories` keys its keyword accumulator on the **raw string**
(`if keyword not in cat_keywords[cat_name]`). Its three sibling functions —
`_merge_seniority`, `_merge_domain`, `_merge_negative` — all normalize with
`key = s.lower()` before deduping. `_merge_categories` is the only one that
does not. The convention already exists in the same file; one function missed
it.

### A3 — A create failure is announced only by a toast that outlives nobody **confirmed**

From URL with a non-posting URL. The API returns a genuinely good 422 message
(#746's work). The FE shows it as a toast — which auto-dismisses — while the
modal stays open holding the draft and displaying **no error at all**.

The from-URL fetch takes 10–20s. A user who looks away for it returns to a
modal that looks untouched, with their URL still in the box and no indication
anything happened. `CreateTargetModal` has no `error` prop; there is nowhere
for the reason to live.

This is the same defect class #742 fixed for the suggest actions — a slow
action whose only failure signal is a 4-second toast — left unfixed on the
create path.

> **Retracted mid-sweep:** I first recorded this as "the UI shows nothing at
> all", having waited 10s before screenshotting and missed the toast entirely.
> The toast does fire, and its wording is excellent. The defect is that it is
> the _only_ surface, not that there is none.

### A4 — `/targets` is the one page that never mentions activation **confirmed**

With all 10 targets Inactive, Home says "Activate a target so we can match
incoming jobs" → **Manage targets**. `/jobs` says "No active targets. Activate
a target to start seeing matched jobs" → **Go to Targets**.

Both send the user to `/targets`, which then says nothing. The page renders a
grid of cards each tagged with a small grey "Inactive" chip, and the word
"Activate" appears nowhere — it is one level down, inside a `⋮` menu with no
accessible name. The two pages that correctly diagnose the problem hand the
user to the one page that does not restate it.

This is the concrete, cap-independent half of the old A3 that #743 deliberately
left open.

---

## B. Confusing / no recourse

| #   | Finding                                                                                                                                                                                                                                                                           |
| --- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| B1  | **After a successful Follow, the only button is "Cancel."** Confirmed in code: the footer renders Create Target only when `mode !== 'search'`, so the Search tab's sole action reads as "undo what I just did". Each row does flip to "Following", but the footer contradicts it. |
| B2  | **Zero-result search dead-ends.** "data engineer" → "No targets match … nobody's set this role up" + _Suggest roles with AI_. The obvious next step — create it with that name — is not offered, and switching to Manual arrives with an **empty** Title. You retype it.          |
| B3  | **Both suggestion panels are undismissable.** `suggestions` and `lateralSuggestions` render with no close control; a 20–50s LLM result sits on the page until a reload. (Prior C2, still open.)                                                                                   |
| B4  | **The `⋮` trigger has no accessible name** — reported by the a11y tree as `button "(unnamed)"`, ten times per page. `JobDetailPanel.tsx:763` already solves this with an `sr-only` span inside the trigger; `TargetCard` does not.                                                |
| B5  | **Reference JD rows carry three unlabeled icon buttons** (upvote / downvote / delete) with no explanation of what a vote does, and the delete confirm — unlike the target delete — never says _which_ JD. With two JDs and identical trash icons, you guess.                      |
| B6  | **Reference JD text is clipped to two lines with no expand.** To read a JD you contributed, you leave the app via the source link. JDs pasted without a URL have no way to be read at all.                                                                                        |
| B7  | **A target can never be renamed.** The `⋮` menu is View jobs / Activate / Delete. With near-duplicates in the list — _Senior Full Stack Engineer_, _Senior Full-Stack Engineer_, _Staff Full-Stack Engineer_, three _Senior Frontend Engineer_ variants — this bites. (Old A4.)   |

---

## C. Correctness questions raised, deliberately not fixed here

### C1 — "Senior Full Stack Engineer" has no fit score, and nothing can recompute one

Persistent across the whole session while every sibling card shows a number.
Pulled the row:

```
target.description        null
target.role_family        null
target.seniority_hint     null
user_target.fit_score            null
user_target.fit_score_reasoning  null
target.activation_status  "polling"
```

It is also the only entry in the catalog search list rendered **without a
description** — consistent with a row that never completed derivation.

**Why the card shows nothing rather than "Building…":** `TargetsList.tsx` seeds
its derive-poller from `activation_status === 'deriving'` only (`pollKey`,
l.335-341), but `isDeriving()` (l.106-112) — the predicate that decides when to
_stop_ polling — also treats `fit_score === null` as deriving. Seed condition
and settle condition disagree, so a row with a null score and a non-`deriving`
status is never picked up on load: no poll, no spinner, no score, forever.

**Not fixed, deliberately.** Aligning the seed to `isDeriving()` would make
this row poll 40 times and never settle. The real fix is a recompute
affordance, which needs a backend endpoint and is spend-bearing — a feature,
not a sweep fix.

### C2 — Merge quality: cross-section duplication and seniority-signal pollution

The same fintech-JD merge that exposed A2 also produced:

- `ACH`, `SEPA`, `PCI-DSS` in **both** a skills category **and** Domain signals.
- Seniority signals gaining **`design`**, **`ship`**, **`own services`** —
  generic JD verbs sitting alongside curated signals like `5+ years` and
  `project leadership`.

Both are extraction-prompt quality, not merge logic — the same defect family as
#749.

**FIXED and released** — `derive_profile.py` `PROMPT_VERSION` v3 -> v4 in #760
(release #769, `main@3f3daae9`). Measured at temperature 0 over the committed
fixture set: leaked terms **5 -> 0**, bare seniority signals **28 -> 5**, perk
signals **1 -> 0**.

The eval that made it safe to touch is `scripts/eval_derive_profile_from_jd.py`,
written first because this prompt had none. It earned its keep immediately: the
FIRST fix drove leaks to zero by making the model _delete_ `ACH`, `SEPA` and
`PCI-DSS` rather than keep them as skills. Only the VOLUME counters caught that,
which is why the rule now names where each term GOES, not merely where it may
not appear.

Residual, deliberately not chased: 5 bare signals remain, and the harness
reports 3 schema failures — the latter is a JSON-mode transport artifact, since
production uses `complete_json`'s forced tool use, where the API validates the
shape before the call returns.

---

## D. Checked and dismissed — do not resurrect

| Claim                                                           | What checking showed                                                                                                                                 |
| --------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| "The reference-JD merge is irreversible with no undo."          | **Wrong.** Deleting the JD re-merges the profile back to its exact prior state. Verified by keyword count and content, 45 → 25.                      |
| "The from-URL failure surfaces nothing."                        | **Wrong.** A toast fires with the full API message. It just auto-dismisses and the modal never shows it. Narrowed into A3.                           |
| "`?tab=jds` hangs on a loading skeleton."                       | **Automation artifact.** `document.visibilityState === 'hidden'` under claude-in-chrome throttles React effects. Not a product defect; not reported. |
| "Card ordering is unstable."                                    | **Not a bug.** New memberships are inserted at the head (`insertEntry`); the order is newest-first and consistent.                                   |
| "Employment-type / location filters are unguessable free text." | **Not a finding.** Both carry concrete placeholders (`e.g. full_time, contract`, `e.g. New York, Remote, Austin`).                                   |

---

## E. Plan

Four PRs into `develop`, then a release. Ordered so the highest-blast-radius
change lands first and can be verified on its own.

| PR  | Findings   | Change                                                                                                                                                                                        |
| --- | ---------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | A2         | **API** — `_merge_categories` dedups keywords case-insensitively, matching its three siblings. First-seen spelling wins; weights average across variants.                                     |
| 2   | A1         | **FE** — `LocalFormat` renders a deterministic UTC string on the server and first client render, then re-renders in the viewer's own locale after mount. Fixes every date in the app.         |
| 3   | A3, B1, B2 | **FE** — `CreateTargetModal` gains a durable inline error; the search-mode footer button becomes "Done"; the zero-result state offers "Create '<query>' manually" and carries the query over. |
| 4   | A4, B3, B4 | **FE** — `/targets` gains a no-active-targets banner with an inline activate path; both suggestion panels become dismissible; the `⋮` trigger gets an `sr-only` name.                         |

Left for a follow-up, with reasons stated above: **C1** (fit-score recompute —
needs a backend endpoint), **C2** (prompt quality — needs a spend-bearing eval),
**B5/B6** (reference-JD affordances), **B7** (rename — `label` feeds
`normalized_label`, the catalog dedup key, so renaming is a data-model
question, not a UI one).

---

## F. What actually shipped

The plan in §E was four PRs. It grew to fourteen, across three releases, because
the release gate's interaction pass and a concurrent session's work both turned
up defects no single PR could have surfaced.

| Finding                                     | Disposition                                                                                                                                                                 | PR         |
| ------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------- |
| A1 dates render tomorrow                    | Fixed — `LocalFormat` two-pass, SSR pinned to `en-US`/UTC                                                                                                                   | #760       |
| A2 case-variant duplicate keywords          | Fixed — `_dedupe_keywords`, applied to the single-profile short-circuit too (the common path)                                                                               | #760       |
| A3 create failure only toasted              | Fixed — durable inline error + draft restore                                                                                                                                | #762       |
| A4 activation never mentioned               | Fixed — no-active-targets banner with an inline activate path                                                                                                               | #762       |
| B1/B2/B3/B4                                 | Fixed alongside A3/A4                                                                                                                                                       | #762       |
| C1 target with no fit score, unrecomputable | Fixed — `stale_target_ids` returns unscored links first, guarded by `_not_deriving()`. **Verified on prod: "Senior Full Stack Engineer" went from permanently null to 52.** | #761       |
| Score nomenclature split                    | Fixed — `/jobs` says "Match score", `/targets` says "Fit". Deliberately two words for two numbers.                                                                          | #763, #764 |
| Active-cap dead end                         | Fixed — `SwapActiveTargetModal`, driven by the server's `active_targets` list so it works at every tier                                                                     | #765       |
| …same dead end from the detail header       | Fixed — the picker was wired only into the cards grid                                                                                                                       | #773       |
| C2 prompt quality                           | **Fixed** — prompt v3 → v4; leaks 5→0, bare seniority signals 28→5, perks 1→0 at temperature 0                                                                              | #760       |
| B5/B6 reference-JD affordances              | Not done                                                                                                                                                                    | —          |
| B7 rename a target                          | **Not done, deliberately.** `label` feeds `normalized_label`, the catalog dedup key. Blocked on a `title_display` split.                                                    | —          |

### Defects found only by assembling the release

None of these were visible in the PR that introduced them.

1. **#749** — #745's prompt had never run against a real model (every test stubs
   the LLM). A live probe returned `Senior Software Engineer` for a JD titled
   `Software Engineer` whose body said "8+ years". The label IS the dedup key,
   so inferring level from prose forks identical titles into two rows.
2. **#751** — `runCreate` reset the suggestion arrays but not #742's new
   empty-panel flags, leaving a stale "your existing targets already cover…".
3. **#752** — #745 discarded its `LLMResult`, so the new inline call never
   reached the cost ledger that `enforce_llm_budget` reads.

### Defects in #766 (concurrent session), found by driving the deployed system

The skill-harvest work shipped in release #769 with three defects, none of which
its own tests could catch, because its fakes returned canned pages.

1. **Livelock** (#770) — the backfill's offset didn't advance past rows matching
   no dictionary term. Under `only_missing` those stay `IS NULL`, pile up at the
   head, and every later page re-reads them. Prod showed `scanned 500,
written 0`. After the fix, draining the catalog wrote **8,603 rows** across
   nine chunks, declining monotonically 1387→628 — the correct shape.
2. **PostgREST's 1,000-row clamp** (#772) — the per-family coverage metric's
   `.limit(20000)` returns 1,000 rows. The "blind-spot monitor" therefore
   described 6% of a 16,625-job catalog and read **0.0% forever**, worse than no
   metric at all. The tell: family totals summing to _exactly_ 1000. Compounded
   because the scan has no `order` while the backfill walks `cataloged_at DESC`,
   so the two sets barely overlapped.
3. The 50k cap added in that same fix was itself a silent truncation — caught in
   self-review and changed to log when the cap is what stopped the scan.

**The durable lesson** is the one already in `feedback_mocks_that_cant_fail`:
each fix here ships a fake that models the _real_ dependency behaviour —
re-evaluating `IS NULL` on every read, truncating any read to 1,000 rows — and
each was falsified against the unfixed code before being trusted.
