# /search flow-stopper sweep — 2026-08-17

New-user exploration of the `/search` routes on **wyrdfold.com** (prod), same playbook as
the onboarding sweep (`docs/onboarding-sweep-2026-08-14.md`), the two `/targets` sweeps
and `docs/ux-sweep-jobs-2026-08-15.md`. Goal: find places where a user is **left without
recourse**, **left confused**, or **cannot finish a task they intended**.

`/search` is the only surface with two distinct audiences, so it was swept twice:

- **Part 1 — logged out** (below, §1–§5). The acquisition funnel: everything a stranger
  can reach, starting from `wyrdfold.com` with no session.
- **Part 2 — signed in** (§6). Members-only affordances, chiefly Add-to-Target and the
  handoff into `/targets`.

Account for Part 2: `hello@danieljoffe.com`. Baseline before the sweep: **10 targets,
all Inactive**. Test writes are listed in §4.

Tracked as **#838** (tracker, with the suggested fix sequence). Findings map to issues:

| Finding                                                                 | Issue    |
| ----------------------------------------------------------------------- | -------- |
| P0-A1 — add-to-target binds nothing the badge will show                 | **#830** |
| P0-1 — dead listing renders the member shell, then the invite wall      | **#831** |
| P0-2 — third "Load more" 422s and wipes results                         | **#832** |
| P1-5 — `extractApiError` never reads `error`                            | **#833** |
| P1-3 + P1-4 — filters can't run, and the no-query page is blank         | **#834** |
| P1-6 — "Sign up free" dead-ends with no waitlist link                   | **#835** |
| P2-7…P2-10, P2-A3…P2-A5 — polish bundle                                 | **#836** |
| `/targets` action-menu a11y (other surface)                             | **#837** |
| §7 — landing page `/signup-mode` 403s, homepage signup CTA stuck closed | **#839** |

Surfaces covered: landing page → `/search` entry, the search box, all three filters
(location / posted-within / salary floor), `Load more` pagination to the hard cap, the
intercepted listing modal, the `/search/[id]` deep-link page, the not-found path for a
dead listing, the `/login` conversion wall, and mobile (400×850).

---

## 1. Flow stoppers — ranked

### P0-1 — A dead listing link strands a logged-out visitor with no way back

Open any `/search/<id>` that no longer resolves — an expired posting, a shared link, a
stale Google result — while signed out. What renders is the **signed-in application
shell**: the full sidebar (Home, Jobs, Search, Targets, Profile, Settings) and a
**"Sign out"** button, shown to someone who has no session. The only recourse offered is
**"Back to dashboard"**, which lands on `/login?next=%2Fdashboard` — the invite-only wall,
which has no link back to `/search`.

Verified end to end on prod, logged out:

| Step                                       | Result                                                    |
| ------------------------------------------ | --------------------------------------------------------- |
| `GET /search/00000000-0000-4000-8000-…`    | "Page not found", rendered inside the member sidebar      |
| Click the only button, "Back to dashboard" | → `/login?next=%2Fdashboard`                              |
| `/login` interactive elements              | logo → `/`, email field, "Send magic link" — nothing else |

Three hops from a normal action (clicking a shared job link) to a total dead end. No
"Back to search", no "Browse jobs".

**Root cause** — a stale invariant, stated in the code. `app/not-found.tsx:13-15`:

> _"The middleware has already redirected unauthenticated users to `/login`, so anyone
> landing here is signed in and just typed a wrong URL."_

That was true when written, and the sidebar is there deliberately (lines 17–20) so an
authed user keeps their nav. Then `/search` and `/search/[id]` shipped as **public**
routes. `notFound()` from the public listing page now falls through to this root
`not-found.tsx`, which still assumes every visitor is authenticated.

**Fix:** the public `/search` segment needs its own `not-found.tsx` — public header, a
"Browse jobs" primary action back to `/search`, and no member sidebar or Sign out.
The root page's assumption should also be corrected now that it is provably reachable
logged out.

### P0-2 — The third "Load more" click 422s and wipes every result on screen

Public pagination is hard-capped at `offset ≤ 40` (`public_search.py:50-51`,
`PUBLIC_MAX_PAGE_SIZE = 20` / `PUBLIC_MAX_OFFSET = 40`) as an anti-enumeration measure.
The client pages by 20 (`JobSearchExplorer.tsx:47`). So offsets 0, 20 and 40 succeed —
60 results — and **`Load more` is still rendered at the ceiling**, because `has_more`
reports whether more rows exist in the corpus, not whether the caller may fetch them.

Click it a third time and the request goes out at `offset=60`, FastAPI rejects it as a
query-validation failure, and the user gets:

- **"Search failed (422)"** — a raw HTTP status code as user-facing copy, and
- **all 60 loaded results disappear from the page**, leaving an empty screen.

Confirmed live on prod, and confirmed at the API directly:

| offset    | status                                    |
| --------- | ----------------------------------------- |
| 0/20/40   | 200 (no duplicate ids across the 3 pages) |
| 60 … 5000 | 422                                       |

The results are **not** lost from state — `loadMore`'s `catch` sets the page-level
`error`, and the results section is gated on
`{!loading && !error && results !== null && …}` (`JobSearchExplorer.tsx:750`). A
_pagination_ failure therefore unmounts the _entire_ result list. The user clicked a
button the app offered them and lost everything they had scrolled through, with no undo
and no way back except re-running the search and remembering to stop at two clicks.

**Fix:** two independent bugs, both worth fixing.

1. Stop offering the click: cap `hasMore` client-side at `PUBLIC_MAX_OFFSET` for
   anonymous callers, and replace the button at the ceiling with an honest
   "That's the first 60 — sign in to see everything" (which is also the conversion moment
   this surface currently lacks).
2. Scope the error: a `loadMore` failure should render an inline retry near the button
   and **must not** unmount results already on screen.

### P1-3 — Filters can be fully configured but will never run, and nothing says so

Set Location = `Remote`, Posted = `Past week`, Salary = `$150k+` with no keyword. The UI
confirms the filters are live — all three controls show their values and the
**"Clear filters"** link appears (`hasActiveFilters`, `JobSearchExplorer.tsx:622`,
rendered at :720). The results area stays blank and the Search button does nothing,
because it is hard-disabled on an empty query:

```tsx
disabled={loading || !draftQ.trim()}   // JobSearchExplorer.tsx:673
```

"Show me remote jobs posted this week over $150k" is one of the most natural things a
visitor will try on a job board, and the app's answer is silence. Nothing states that a
keyword is required.

Made worse by the two commit paths being inconsistent: `changeRecency` / `changeSalary`
commit to the URL **immediately** (:585-588), so the selects appear responsive and
genuinely change application state, while location only applies via the disabled Search
button. The visitor gets partial feedback that the app is working.

**Fix:** either require the keyword _visibly_ (helper text on the disabled button, e.g.
"Add a keyword to search") or — better, and what the subtitle already promises — let
filters run against an empty query and browse the pool.

### P1-4 — `/search` with no query renders nothing at all

The bare `/search` page is blank below the controls: no guidance, no example queries, no
popular searches, no sample of the corpus. A visitor arriving from the landing page's
"Search jobs" link sees an empty page and has to guess what to type.

This is not a missing empty-state so much as an unreachable one. On an empty query the
effect bails early with `setResults(null)` (`JobSearchExplorer.tsx:523-528`), and the
entire results section is gated on `results !== null` (:750) — so **no branch renders**.
The comment directly above the bail says the opposite of what happens:

> _"An empty query renders the honest empty page."_ (:517)

Compounding it: **"Browse more jobs"** on the listing detail page links to `/search`
(no query) — so the one navigational escape hatch offered on a listing lands the user on
this blank screen.

**Fix:** render a real zero-state — a few starter queries, or the most recent postings.
The corpus is the product's best sales pitch and currently a stranger sees none of it.

### P1-5 — Every public-search error surfaces a raw HTTP status code

`extractApiError` reads only the `detail` key (`lib/extractApiError.ts:38-40`, falling
back to `` `${fallback} (${res.status})` `` at :30). The public search BFF normalises its
errors to a **different** key — `{ error: message }`
(`api/public/search/route.ts:132`). The two never meet, so the carefully-written upstream
message is discarded on every failure and the user sees `Search failed (<status>)`.

That is how P0-2 surfaces as "Search failed (422)". The BFF had actually produced
_"Something went wrong. Please try again."_ — itself wrong advice in that case, since
retrying can never succeed.

**Same defect hits the 429 path.** `/public/search` is rate-limited `10/minute;60/hour`
per IP (`public_search.py:71`), and the BFF deliberately preserves both the upstream
message and the `Retry-After` header (route.ts:129-135) — all of which
`extractApiError` throws away, leaving a rate-limited visitor with `Search failed (429)`
and no idea to wait a minute.

_Not observed, inferred from the shared code path._ I chose not to burn the `60/hour`
per-IP budget to demonstrate it, because doing so would have locked this IP out of the
surface still under test. The 422 case proves the mechanism empirically.

**Fix:** teach `extractApiError` to read `error` as well as `detail` — it is the shared
helper for every fetch in the app, so the public BFF's shape being invisible to it is
likely to bite elsewhere too.

### P1-6 — "Sign up free" dead-ends on an invite wall with no waitlist link

The `/search` header's primary CTA reads **"Sign up free"**. It points at `/login`
(`href="/login"`, same as the "Sign in" link beside it), a page titled **"Sign in"**.
Enter any address that isn't already invited and the response is:

> _"This email isn't on the beta list yet. If you think it should be, reply to your
> invitation."_

A stranger who clicked "Sign up free" has no invitation to reply to. `/login` offers no
waitlist link, no "request access" — its only interactive elements are the logo, the
email field and "Send magic link". The landing page **does** have a waitlist form; the
page the CTA sends people to does not.

So the funnel is: guest searches → finds a role they want → clicks the button promising
free signup → is told they aren't on a list they've never heard of → dead end. The same
"Sign up free" link appears twice more inside the listing detail upsell.

**Fix:** cheapest high-value change in this report. Point "Sign up free" at the waitlist,
or add "Not invited yet? Join the waitlist →" to `/login`. Also worth aligning the copy —
"Sign up free", "Join the waitlist" and "invite-only private beta" are three different
promises for the same door.

---

## 2. Confusion / discoverability (lower severity)

**P2-7 — No result count exists anywhere, and the 60-row ceiling is invisible.** A visitor
cannot tell whether "frontend engineer" matched 20 roles or 2,000, nor that they are
being served the first 60 of a much larger corpus. Worth flagging that this is _not_ a UI
oversight: the API deliberately does not compute a total —
`JobSearchResponse.count` is "the size of THIS page (not the total corpus match count,
which a title-ranked search doesn't compute)" (`models/job_search.py:56-58`). Showing a
total means adding a COUNT query, which is a real cost decision, not a label change. A
cheaper honest version: "Showing the first 60 matches".

**P2-8 — Listing detail is a 3-line snippet cut mid-word, but the page promises "the
details".** The `/search` subtitle reads _"Open any role for **the details** and a link to
the original posting."_ What opens is a truncated preview — `…provides the guardrails for
safe AI governa…` — with no "read more". This is by design (the public projection never
selects the JD body — `public_search.py` module docstring), but nothing tells the user
that, and the upsell box next to it explains match scoring and résumé tailoring rather
than the truncation they are actually looking at. Either the subtitle should stop
promising details, or the upsell should say "Sign in to read the full description".

**P2-9 — Page titles use the raw company slug.** The listing tab title renders
`Software Engineer - Frontend at qualified-health-pbc` — the slug, not the display name
("Qualified Health Pbc") shown in the card two lines below. This is the SEO surface and
the shared-link preview.

**P2-10 — Keyword relevance is loose enough to read as broken.** `frontend engineer`
returns, on page one, `Seasonal Associate, Cloud Engineer`, `Software Engineering
Manager`, `DevOps Engineer, Senior` and `Systems Engineering Lead`; deeper pages add
`Software Engineering Intern (Winter)` and `Android-Savvy CNO Developer`. Title-ranked
matching on the token "engineer" explains it, but with no relevance score, no sort control
and no result count, a visitor has no way to tell a weak match from a bug.

---

## 3. What works well

- **The empty-_results_ state is genuinely good** — it names the query, acknowledges the
  filters (_"No roles match "…" with these filters yet"_), gives distinct advice for the
  filtered and unfiltered cases, and leaves "Clear filters" in reach
  (`JobSearchExplorer.tsx:752-763`). The contrast with P1-4 is the story: careful guidance
  was written for the has-query branch and none for the no-query branch.
- **Modal ↔ URL integration is solid.** A card click soft-navigates to `/search/<id>`,
  the intercepted modal renders over the grid, and **Escape restores the exact result set
  and scroll position**. `router.replace` keeps back/forward sane (:561-573).
- **Deep links work and are shareable** — `/search/[id]` renders standalone with a
  "Browse more jobs" link, and "View original posting" resolves to the real ATS URL
  (verified: Ashby).
- **Pagination is correct within the cap** — 0 duplicate ids across offsets 0/20/40.
- **Ordering is stable** — back-to-back identical queries returned byte-identical id
  lists (see the correction in §5).
- **Mobile (400×850) holds up** — single column, controls reflow cleanly, the modal is
  readable and dismissible. No layout breakage found.
- **The not-on-the-list message is honest and specific** — it just needs somewhere to
  send people (P1-6).

---

## 4. Test writes made against the prod account

Part 1 (logged out) made **no writes**. One deliberate probe is worth recording:

- Submitted `guest-flow-test@example.com` to `/login` to observe the non-invited path.
  **No email was sent** — the address is rejected before dispatch. `example.com` is
  IANA-reserved precisely so this kind of test reaches no real recipient.
- The account was **signed out** at the start of Part 1 and signed back in by the owner
  for Part 2.

Part 2 (signed in) made these writes, **all undone**:

| Write                                                                                                                                                      | Undone                                                 |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| Added _Frontend Developer (React) – Junior / Middle_ (Careerswift.ai) to **Founding Engineer** — twice, once via UI and once via API while isolating P0-A1 | ✅ Removed via `/jobs` → status `Saved` → batch Remove |
| Added _Founding Full-Stack Engineer_ (Clera) to **Founding Engineer**                                                                                      | ✅ Removed in the same batch                           |
| Activated target **Founding Engineer** (to rule out activation as the cause of P0-A1)                                                                      | ✅ Deactivated                                         |

Verified after cleanup: `/jobs?status=saved` → "No jobs found"; `/targets` → 10 targets,
all Inactive, "No targets are active — nothing is being matched". **Matches the baseline.**

One residue that cannot be undone: Founding Engineer's _Updated_ date moved 8/15/2026 →
8/17/2026 as a side effect of the activate/deactivate cycle. Keyword and category counts
are unchanged (3 / 26).

---

## 5. Method notes

**A correction, recorded because it nearly became a finding.** Two identical
`frontend engineer` searches minutes apart returned visibly different result lists (four
Cisco roles appeared mid-page on the second), and I initially took that for
non-deterministic ordering — the same class of bug as the `/jobs` sort × load-more defect.
It is not. Fetching the same query twice back-to-back returned **byte-identical id
lists**; the difference was new postings ingested by the poller between the two searches.
Retracted before it reached the report. _Two screenshots minutes apart are not a
determinism test against a corpus that ingests continuously._

**On not reproducing the 429** (P1-5): demonstrating it would have consumed the
`60/hour` per-IP allowance and locked this IP out of the surface still under test. The
code path is shared with the 422 already proven live, so it is reported as inferred and
labelled as such rather than claimed as observed.

**Caps were read from source, not guessed from behaviour.** The 422 boundary was found by
probing offsets (40 ok, 60 fails) and then confirmed against
`public_search.py:50-51` — the constants, not the inference, are what §1 cites.

**A slow search that wasn't.** The authed `/search` appeared to hang for 25–35 s behind a
"Searching…" spinner. Measured directly from the page, `/api/jobs/search` returns in
**~500 ms**. The delay was browser-automation throttling, not the product. Not reported as
a finding.

---

## 6. Part 2 — signed in

Same account, all ten targets Inactive at the start (the app's own banner: _"No targets are
active — nothing is being matched"_). The authed surface swaps the search endpoint to
`/api/jobs/search` (`JobSearchExplorer.tsx:431`) and adds two things to each card: a
membership badge, and the Add-to-Target action that reaches into `/targets`.

### P0-A1 — "Add to target" never shows the target you picked

The single worst defect found in this sweep, because the user's deliberate action leaves
no trace they can find.

Adding a job to a target returns **HTTP 200**, and the detail panel immediately flips to a
fully-bound state — `✓ In "Founding Engineer"`, plus **"See how you match"**, **"Tailor a
résumé"** and **"Add to another target"**. Reload the page and all of it is gone; the
listing is back to _"Add to a target to unlock fit analysis and resume tailoring."_

Two adds, both against the same target, both HTTP 200:

| Job added                                  | Score returned | `target-membership` afterwards                         |
| ------------------------------------------ | -------------- | ------------------------------------------------------ |
| Frontend Developer (React) – Junior/Middle | `0`            | `{}` — nothing at all                                  |
| Founding Full-Stack Engineer               | `43`           | **`Senior Frontend Engineer`** — a target never picked |

The second row is the clearest statement of the bug: the badge names a target the user
did **not** choose (one the job already belonged to), while **Founding Engineer — the
target they actually picked — never appears at all**.

**Root cause: two different definitions of "in a target".** `add-to-target`
(`jobs.py:3278-3382`) stage-2 scores the pair, force-includes it, and marks the
`user_job` as `saved`. `target-membership` deliberately requires _more_ than a scores row
— per its own docstring, the row must be non-excluded **and `promising`** (the **stage-1**
admit) **and** pass the #277 family gate:

> _"a scores row alone only proves the pair was once evaluated"_

A manual add writes a stage-2 score and never sets the stage-1 `promising` flag, so the
badge logic excludes it by construction. The comment in `jobs.py` asserts the opposite —
_"the job IS scored and will show under the target"_ — which is true of the score row and
false of everything the user can see.

Ruled out along the way:

- **Not an inactive-target problem.** Activating Founding Engineer did not change the
  membership response.
- **Not a lost write.** The job is genuinely saved — it is findable under `/jobs` →
  status `Saved`. It is only the _target association_ that is invisible.
- **Not visible under the target either** — neither job appeared in the Founding Engineer
  tab on `/jobs`.

**Fix:** make the two definitions agree. Either `add-to-target` should admit the pair into
the pipeline (set `promising`, and treat a deliberate user add as bypassing the family
gate — the user has explicitly overridden the machine's judgment), or the badge should
report user-added membership as a distinct, first-class state. Shipping the optimistic
flip while the read path disagrees is the worst of both.

### P1-A2 — The add is the only significant action with no confirmation

Nothing is shown on success — no toast, no persistent state. Contrast the same
account's target activation, which fires a clear **"Target activated"** toast. The only
feedback for an add is the optimistic panel flip, and per P0-A1 that flip is wrong. When
the user returns to the results grid, the card still reads "Add to target", so the app's
two surfaces contradict each other within one click.

### P2-A3 — Two identical "Add to target" controls, one of which is decorative

On the results card, "Add to target" (with a `+` icon) is a **`Badge`, not a button** —
`JobSearchExplorer.tsx:404-407`, and deliberately so per the comment at :392-395
("presentational — the card itself opens the modal, where the action happens"). Clicking
it opens the listing modal, which contains a real `Button` with the _same label and the
same plus affordance_. The intent is defensible; the visual treatment gives the user no
way to tell the decorative one from the functional one.

### P2-A4 — The target menu clips its own options

Target names are truncated mid-word with no ellipsis, and the menu overflows outside the
dialog's bounds. "All Levels: Fullstack Software Engine…", "Senior Frontend Engineer –
Performa…" and "Senior Frontend Engineer – Web Perf…" are all cut. With two pairs of
near-identical names in this account (`Senior Full Stack Engineer` vs `Senior Full-Stack
Engineer`), clipping is how a user picks the wrong target.

Purely a CSS problem — the accessible names are complete and correctly exposed as
`menuitem`s, which is worth stating: the menu itself is built properly.

### P2-A5 — Salary formatting differs between card and detail

The same job renders `$65k–$85k` on the search card and **`$65000–$85000`** in the
detail chip. The compact form is used everywhere else in the app.

### The public depth-cap bug (P0-2) also affects members, more slowly

The authed route caps at `MAX_OFFSET = 250` (`services/job_search.py:62-67`) against the
client's page size of 20 — so a signed-in user hits the identical 422-and-wipe at the
**13th** "Load more" instead of the 3rd. Same component, same render gate, same result.
Any fix must cover both endpoints.

### What works well (signed in)

- **The bind→unlock flip is genuinely good design** — the moment a job is bound, the panel
  replaces "Add to target" with the three things you'd actually want next (see how you
  match / tailor a résumé / add to another target). It is only let down by not persisting.
- **Membership badges for real pipeline members are accurate and useful**, and the picker
  honestly labels every target `inactive` rather than pretending.
- **The authed subtitle sets correct expectations** — _"These results aren't scored against
  your profile — head to Jobs to see how you match"_ — a good disambiguation of `/search`
  vs `/jobs`.
- **Removal is a model flow**: multi-select, a confirm that states scope and reversibility
  (_"Remove 2 jobs from your targets? They will stop appearing here. You can undo this."_),
  and it worked exactly as described.

### Two things left unresolved

- **Add-to-target from inside the modal is untested.** My first attempt there (clicking the
  menu item by coordinate) closed the modal and wrote nothing, but the same action on the
  standalone `/search/[id]` page — clicked by element reference — wrote correctly. Two
  other coordinate clicks in that modal also mis-landed, so this is most likely a browser-
  automation artefact rather than a product bug. **It is recorded as unproven, not as a
  finding** — worth one manual click to confirm.
- **`/targets` action-menu items expose no accessible names** (the menu reads as three
  unnamed `menuitem`s), so a screen-reader user cannot tell Activate from **Delete**, which
  sits directly beneath it. Out of scope for this sweep and on an already-swept surface,
  but it is a destructive action one row away from an unlabelled control, so it is noted
  here rather than dropped.

---

## 7. Railway log review (deploy `73f3275a`)

Read after the sweep, to see whether the API had clues the UI didn't surface.

**Window caveat.** Retention on this deployment is short: the container started 23:33 and
the logs cover 23:33 → 00:46 — almost exactly the sweep session. All 132 HTTP requests in
it are mine. So this is a review of _my_ traffic against the API, not a survey of organic
production traffic. Everything below is provable from code plus log together; nothing is
extrapolated from volume.

### New defect — `/signup-mode` 403s from the landing page (#839)

`GET /signup-mode` returned **403 three times and 200 three times** in the same window,
split exactly by caller:

| Caller                                                       | Sends `bffSecretHeader()` | Status  |
| ------------------------------------------------------------ | ------------------------- | ------- |
| `api/signup-mode/route.ts:17-19` (login form, via BFF)       | yes                       | **200** |
| `(public)/page.tsx:116-118` (landing page, server component) | **no**                    | **403** |

The endpoint is gated by `Depends(require_bff_secret)` (`waitlist.py:118-121`), which 403s
on a missing/mismatched `x-wyrdfold-bff` header (`dependencies.py:205-207`).
`signupMode()` swallows the 403 into its fail-safe and returns `'closed'`, so

```ts
const signupOpen = mode === 'saas' && (await signupMode()) === 'open';
```

is **permanently false** — the homepage can never show the sign-up CTA, even after the
operator flips signup open. The failure mode is the safe one, so nothing surfaces it.

It is an omission rather than a design choice: all six other frontend callers of BFF-gated
endpoints send the header, and `bffSecretHeader`'s own docstring names `signup-mode` as one
that requires it. The BFF route has a test; the landing-page caller does not.

### Root cause contributed to #652 (Phase-1 `title_prefix` drops)

The mismatch sample logging shows the check rejecting a **semantically correct** echo:

```
id=1 title='(1498) Sr. AEM Backend Developer' prefix='Sr. AEM Backend'
```

The title opens with a requisition number; the model omits it (as all three prompt examples
imply it should); `_prefix_matches_title`'s strict `startswith` (`title_triage.py:243-246`)
fails. One badly-titled posting burned a verdict across **five targets in one cycle**. That
issue's title says "undiagnosable from logs" — no longer true.

### Supplemented

- **#832** — 12× `422` on `/public/search` confirmed server-side, 4–31 ms each, rejected at
  query validation before any DB work. No 5xx in the window, so the result-wipe is purely
  client-side rendering.
- **#604** — `/jobs` at **3.5–4.1 s** from the API's own `slow_request` logging, on one
  active target, single user, no concurrent load. Still live, and worse than the
  poller-contention framing it was filed under.

### Observed, deliberately not filed

- `candidate window saturated at 1000 rows (… sort=score, tier=pending)` on every `/jobs`
  load. Post-#813 the window is drawn in ranking order, so serving the top 1,000 is
  intended. Noted on #604 (same 4-second query); could not substantiate it as a bug.
- `poll MatX returned 0 jobs but 8 active rows exist — skipping stale-archive pass` — the
  non-JSON-boards guard doing its job. A board-health signal, not a defect.
- `workday takeda … returned 422 at offset 0` — one board rejecting at offset 0, handled
  without incident. One occurrence in 70 minutes isn't enough to call it.
- `forced tool_call missing for return_TitleTriageResponse … retrying once` — twice; the
  salvage/retry path behaved as designed.

**Nothing in the logs contradicted a finding in §1–§6.**
