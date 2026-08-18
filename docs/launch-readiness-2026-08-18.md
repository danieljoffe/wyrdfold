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

### #841 — a new account cannot complete onboarding (launch blocker)

> **This section was rewritten.** It previously claimed the free tier bills the
> operator. That was wrong in both halves — see `docs/decisions.md`,
> "reading config is not tracing the path".

Free accounts spend **nothing** of the operator's. They are refused with a 402
before any LLM client is constructed: `llm/__init__.py:224` raises on an OR —
`BYOK_REQUIRE_USER_KEYS` **or** the caller's plan being BYOK, which the saas
free tier always is. The flag is unset; the plan branch fires on its own.
Background is deferred the same way (`poller.py:1454`). The #5 cost firewall is
intact, and `20260721060000_open_signup_abuse_controls.sql`'s premise holds.

The real problem is the mirror image, verified live on a genuinely new account:

- `upload-resume` → **402**, `targets/suggest` → **402**,
  `conversation/next-probe` → **402**
- all three say _"Add your OpenRouter API key in Settings to use AI features."_
- `/settings` → Account says _"Bring-your-own-key isn't available on this
  server"_ — `BYOK_MASTER_KEY` is not set

**The error instructs the user to do the one thing the app states it cannot
do.** Onboarding dead-ends at step 2 of 4. The only working exit is to
subscribe.

Three shapes, unchanged, but chosen for a different reason — not "bound runaway
spend" but "give a new user something to do before they pay":

1. **Set `BYOK_MASTER_KEY`** and re-enable BYOK — makes the free plan's existing
   design function. Still asks a consumer for a provider key before the product
   has done anything, and makes you custodian of those keys.
2. **Bounded host allowance for free** — frictionless, costs real money per
   signup, and needs an _aggregate_ ceiling. Note `global_llm_daily_budget_usd`
   is consulted **only in `poller.py`**, never on the HTTP path.
3. **No free tier** — but `entitlements_for(None)` returns `free`, so removing
   the tier does not remove the state, and onboarding must then stop routing
   every new user through résumé parsing and target derivation.

A **time-boxed trial** remains worth considering against the three.

How the failure is communicated is tracked separately: #857 (onboarding
mishandles the 402 in three places — one reports success, one says "try again"
forever) and #858 (`/settings` contradicts itself three ways).

### #861 — production runs Stripe in test mode (launch blocker)

`STRIPE_SECRET_KEY` is an `sk_test_` key, so **no real customer can
subscribe** — a live card against a test-mode Checkout is rejected, and Stripe
renders a customer-facing "Sandbox" badge on the payment page. With #841 walling
free accounts, there is currently no path by which anyone becomes a paying user.

Root cause is topology: one Railway project, one environment named
`development`, serving production. The test key is correct _for_ a development
environment; there is nowhere else for it to live. Fix the split, not the value.

Used deliberately while it lasted: because prod was in test mode, the full
checkout → webhook → `user_profiles.plan` chain was exercised end to end at zero
cost and **works** — `stripe_customer_id` written, plan flipped to `starter`,
allowance switched from the generic `$5` fallback to the plan's real quota
(which is also direct evidence for #858). When live keys land, the only untested
variable is the key itself.

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
2. ~~**A genuinely new account.**~~ **Done** — two fresh accounts walked
   (`danny@dannys.io`, then `danny+test_856@dannys.io` via a real invite).
   Confirmed the hook does `lower(email)` with **no plus-address stripping**, so
   `user+tag@domain` is a distinct identity. The empty states themselves hold up
   — `/profile` and `/targets` explain the dependency honestly and `/dashboard`
   correctly redirects a profile-less account to `/onboarding`. **The empty
   states were never the problem; the walls behind them are** (#841, #857,
   #858).
3. **The end-to-end journey as one flow** — signup → onboarding → target →
   match → tailored résumé. Routes were swept in isolation, and #830 lived in a
   seam between two of them. _Blocked until now by #841_ — a free account cannot
   get past onboarding step 2. Unblocked by putting a test account on Starter
   through a real Checkout.
4. **The paid path.** Now exercised end to end (see #861): checkout session →
   Stripe → webhook → `stripe_customer_id` → plan flip → UI. It works. What is
   still untested is the same path against **live** keys, and the
   `customer.subscription.deleted` / downgrade branch.

---

## 4. Open findings

**Launch blockers:** #841 (new accounts can't onboard) · #861 (prod Stripe is in
test mode, so nobody can pay) · #860 ("Confirm signup" still on
`{{ .ConfirmationURL }}` — breaks cross-device the day signup opens).

**Flow stoppers:** #857 (onboarding mishandles the 402 in three places) · #858
(`/settings` contradicts itself) · #863 (nothing shown after a successful
payment; the plan flip races the redirect) · #831 (dead listing strands
logged-out visitors) · #832 (Load-more wipes results) · #833 (raw status
codes) · #834 (blank no-query page) · #835 ("Sign up free" dead end) · #843
(Trends filter skips three panels).

**Observability:** #862 — production discards **every `logger.info`** (84 call
sites, incl. billing plan changes and all of `scheduler.py`'s outcome
reporting). `init_logging` sets the root level only on the JSON branch, and
`LOG_FORMAT` is unset.

**Fixed this session:** #856 (invite links discarded — verified fixed with a
real invite) · #859 (the BYOK defer log named the wrong trigger).

**Polish:** #836 · #844 · #837.

**Pre-existing:** #652 (now root-caused: requisition-number title prefixes
break the strict `startswith`) · #795 · #604 · #634 · #698 · #470 · #467 · #1.

**Capability gap, not a bug:** email and SMS notifications are unavailable in
production. The UI is honest and correctly disabled, but the landing page sells
background delivery. SMS is tracked by #1.

---

## 5. Method notes worth keeping

- **Reading config is not tracing the path.** #841 was filed backwards because
  the settings in a branch were read instead of walking the call path to it. A
  gate upstream can make an entire downstream analysis moot.
- **Read the tests before claiming they missed it.** The same issue assumed a
  coverage gap that did not exist — the free-tier refusal was already pinned,
  with the flag set to `False` explicitly.
- **An absent log line is evidence.** #862 was found because a webhook succeeded
  and its log line didn't appear. A correct outcome is exactly what makes a
  missing log easy to dismiss.
- **Ask the operator when logs can't discriminate.** `GET /auth/callback 307` is
  identical for success and failure, and query strings aren't logged. One
  question settled what log-mining could not.
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
