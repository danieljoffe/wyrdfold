# Launch readiness — state of play, 2026-08-18

Working record of the 2026-08-17/18 push toward **going public and opening
signup**. Companion to `docs/ux-sweep-search-2026-08-17.md` (the `/search`
sweep) and `docs/decisions.md` (the war stories this produced).

The goal reframed the work: the app has been diagnosed route-by-route in its
**invite-only** configuration, and that is not the configuration it is about to
be promoted into.

> Cost, spend and margin figures are deliberately omitted — this repo is
> public. They are held privately.

---

## 1. Released

Two releases, both verified in prod rather than assumed.

### `main@ffbbf5b2`

| PR   | What                                                               |
| ---- | ------------------------------------------------------------------ |
| #840 | landing page sends the BFF secret on `/signup-mode`                |
| #845 | stop Phase-1 grading catalog targets nobody is pursuing            |
| #847 | read remote + employment type from Ashby / Lever / SmartRecruiters |
| #848 | promote a board-stated remote location into `is_remote`            |

**Verified:** `/version` matched `main` HEAD (the deploy discriminator that
works — `openapi.json` only moves when the schema does). `/signup-mode` flipped
**403 → 200** in the HTTP logs. The five catalog target ids that appeared
repeatedly in Phase-1 logs before the release appeared **zero** times after,
while the one sponsored target kept grading correctly.

### `main@87d31ea6`

| PR   | What                                                                       |
| ---- | -------------------------------------------------------------------------- |
| #851 | Phase 2 stops emitting logistics; the tagger stops clobbering board values |
| #852 | salvage a tool call written as bare JSON in `content` (#850)               |

**Verified:** `salvaged 'return_TitleTriageResponse' from a prose JSON tool
call` fired on live traffic with **0 `MissingToolCallError`** (the pre-deploy
window had 4 plus a dead batch). **46 scores created since deploy carry zero
logistics.**

**Still unproven:** #851's tagger-deference half. Too few jobs had ingested,
and headcount alone can't prove it — before #851 the tagger overwrote
everything, so the real discriminator is a board value that _disagrees_ with
what the tagger would infer. Needs Ashby/Lever volume.

---

## 2. Decisions outstanding

### #841 — the free tier bills the operator (launch blocker)

BYOK is off, `BYOK_REQUIRE_USER_KEYS` is unset, and `DEPLOYMENT_MODE=saas`. The
config's own docstring calls `True` "the hosted-multi-tenant posture, so
strangers can't spend the operator's credits" — and it is off in the hosted
multi-tenant deployment. The plan model assumes free = BYOK ("there is nothing
of the host's to spend"), which is false here.

Three shapes, none free of cost:

1. **Require BYOK** — restores the designed firewall exactly. But
   `BYOK_MASTER_KEY` is **not set**, so this isn't a flag flip: it means
   generating a master key and becoming custodian of every user's provider key.
   And it asks for an API key before the product has done anything, inverting
   the "runs while you don't" pitch at step one.
2. **Bounded host allowance for free** — frictionless, costs real money per
   signup, and needs an _aggregate_ ceiling (the per-user rails already exist).
3. **No free tier** — the current lean. Note `entitlements_for(None)` returns
   `free`, so removing the tier does not remove the state: an unpaid account
   still has to be defined, and if it can't run LLM work, onboarding
   dead-ends at step one.

A **time-boxed trial** bounds exposure by duration rather than rate and keeps
onboarding intact — worth considering against the three.

Also invalidated: `20260721060000_open_signup_abuse_controls.sql` scoped its
disposable-email blocklist as _secondary_ on the premise that BYOK was the
primary cost control. That premise does not hold.

### #830 / #842 — one question, two symptoms

"Which targets count?" is answered differently in three places:
`add-to-target` writes a **stage-2** score; `target-membership` requires the
**stage-1** `promising` admit plus the family gate; dashboard insights span
**all** targets while `/jobs` filters to **active** ones. Decided direction: a
deliberate user add enters the pipeline and **bypasses the family gate** — an
explicit user action outranks the classifier. Not yet built.

---

## 3. Never tested — the real gap

Everything below is a _configuration_ gap, not a defect. Each is invisible in
the current setup, which is exactly why it needs exercising before launch.

1. **Open-signup mode.** Nothing has exercised `signup_mode='open'`,
   `shouldCreateUser: true`, the auth hook's open branch, or the
   disposable-domain blocklist. The switch has **four parts in three places**
   and each fails safe, so it can be half-flipped and look fine:
   `app_settings.signup_mode` · the auth hook (**hosted dashboard**) · GoTrue
   rate limits (**hosted dashboard**) · the FE probe (fixed in #840).
   Two of those cannot be verified from the repo.
   _Method:_ operator flips open → verify the CTA changes, a non-invited
   address is accepted, and a disposable domain is still rejected → flip back.
2. **A genuinely new account.** Every sweep ran against an account with ten
   targets and months of history, so first-run empty states on `/profile` and
   `/settings` are unverified. Note the hook does `lower(email)` with **no
   plus-address stripping**, so `user+tag@domain` is a distinct identity —
   aliasing works on our side; only mail deliverability is unknown.
3. **The end-to-end journey as one flow** — signup → onboarding → target →
   match → tailored résumé. Routes were swept in isolation, and #830 lived in a
   seam between two of them.

---

## 4. Open findings

**Flow stoppers:** #831 (dead listing strands logged-out visitors) · #832
(Load-more wipes results) · #833 (raw status codes) · #834 (blank no-query
page) · #835 ("Sign up free" dead end) · #843 (Trends filter skips three
panels).

**Polish:** #836 · #844 · #837.

**Pre-existing:** #652 (now root-caused: requisition-number title prefixes
break the strict `startswith`) · #795 · #604 · #634 · #698 · #470 · #467 · #1.

**Capability gap, not a bug:** email and SMS notifications are unavailable in
production. The UI is honest and correctly disabled, but the landing page sells
background delivery. SMS is tracked by #1.

---

## 5. Method notes worth keeping

- **A measurement is a claim.** Three predicates invented three bugs in one
  day — see `docs/decisions.md`. State the predicate; check it against the
  code's documented behaviour before reporting a number.
- **Find the last writer.** Reading the board was a complete no-op until the
  tagger stopped overwriting it. When a column has several writers, the last
  one decides.
- **Release gate: hunt interactions.** The per-PR diffs were all clean; the
  question worth asking was whether #845 skipping grading would stop #847/#848
  ever writing. (It doesn't — verified from prod, not from reading.)
- **`railway logs` is a first-class diagnostic.** Both #839 and #850 were found
  by reading them, and neither was reachable from tests.
- **E2E flakes on concurrent CI.** The job binds fixed host ports for the local
  Supabase stack and collides on 54324 when two PRs run at once. Re-run it;
  don't change code.
