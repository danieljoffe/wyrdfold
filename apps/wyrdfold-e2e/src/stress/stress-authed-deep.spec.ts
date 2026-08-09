/* eslint-disable playwright/no-networkidle, playwright/no-wait-for-timeout, playwright/no-skipped-test, playwright/no-conditional-in-test, playwright/no-conditional-expect, @typescript-eslint/no-empty-function --
 * Timing harness, not a functional spec: 'networkidle' IS the measurement,
 * fixed settle waits bracket trace capture, per-action pass/fail lives in the
 * JSONL ledger + coverage gate, and several actions branch on live prod state
 * (a learning run may or may not yield suggestions to reject). */
import { expect, test } from '@playwright/test';
import { timedAction } from './timing';

/**
 * Deep coverage pass — every action the earlier sweeps excluded as
 * destructive/spend-heavy, plus the gaps the 2026-08-08 manual prod drive
 * turned up (score-floor, country filter, funnel chips, batch bar, cover
 * letter parity with resume, target CRUD, onboarding round-trip).
 *
 * Owner authorised a full-blast run. Restore discipline is still absolute:
 *  - destructive target work happens on a THROWAWAY target this spec creates
 *    and deletes, never on one of the owner's six real targets;
 *  - the posting this spec deletes is one it added itself by URL;
 *  - approve is always followed by unapprove, status flips are reverted,
 *    onboarding reset is followed by complete.
 *
 * Deliberately NOT fired, with the auth/validation gate asserted instead:
 *  - POST /api/jobs/poll — a global all-tenant, cost-bearing fan-out gated on
 *    CRON_SECRET. Asserting the 403 for a session caller is the audit-#29
 *    privilege-escalation guard and is worth more than triggering a real poll.
 *  - POST /api/email/target-paused — would send real email.
 *  - Stripe: sessions are created (no charge); nothing is ever submitted.
 */

const REAL_TARGET_ID = '012202b0-e6bd-4617-b9cb-226648e8218e';
const SCRATCH_TARGET_LABEL = `E2E Coverage Scratch ${Date.now()}`;
/** A live posting URL used for the manual-add → delete round trip. */
const MANUAL_ADD_URL =
  'https://job-boards.greenhouse.io/nearform/jobs/7831952003';

// NOT serial. The config already pins workers:1 + fullyParallel:false, so
// tests run in declaration order anyway — but under `mode: 'serial'` a single
// flaky locator in test 3 SKIPS tests 4-8 and silently guts the sweep's
// coverage (it did exactly that twice on 2026-08-08). Every test below
// re-establishes the state it needs, so none of them depend on the last one.

let scratchTargetId: string | null = null;
let scratchUserTargetId: string | null = null;
let manualJobId: string | null = null;
let draftJobId: string | null = null;
let promisedSaved = -1;
let locationQuery = '';
/** The target's activation state before this project touched anything, so the
 *  teardown restores it instead of forcing it off. Captured on first use. */
let wasActiveBeforeSweep = false;
let capturedOriginalState = false;

/**
 * `stress-authed.spec.ts` ends with `targets.deactivate-restore`, so by the
 * time this project runs the owner has NO active target and /jobs is empty —
 * the first run of this file died waiting 90s for a score badge that could
 * never appear, and serial mode then skipped five whole tests. Re-activate
 * here and hand the state back at the end.
 */
async function targetIsActive(
  page: import('@playwright/test').Page
): Promise<boolean> {
  const res = await page.request.get(
    `/api/targets/${REAL_TARGET_ID}/user-target`
  );
  if (!res.ok()) return false;
  const body = (await res.json()) as {
    is_active?: boolean;
    user_target?: { is_active?: boolean };
  };
  return Boolean(body.is_active ?? body.user_target?.is_active);
}

async function ensureJobsPopulated(page: import('@playwright/test').Page) {
  // IDEMPOTENT ON PURPOSE. Activation is not a free read: it spawns a poll
  // fan-out across the target's sources. This helper used to POST /activate
  // unconditionally at all three call sites, and the teardown deactivated
  // unconditionally on top of the base spec's own deactivate — so one sweep
  // cycled the target ~6 times and the API logged
  // `poll_sources_for_target: … deactivated mid-fan-out — aborting remaining
  // sources` ten times (prod, 2026-08-08). The sweep was measuring an app it
  // was actively destabilising, and it left real poll work half-done.
  //
  // Ask first; only mutate when the state is actually wrong.
  const active = await targetIsActive(page);
  if (!capturedOriginalState) {
    wasActiveBeforeSweep = active;
    capturedOriginalState = true;
  }
  if (!active) {
    await page.request.post(`/api/targets/${REAL_TARGET_ID}/activate`);
  }
  const deadline = Date.now() + 90_000;
  while (Date.now() < deadline) {
    const res = await page.request.get(
      '/api/jobs?status=new&sort=score&order=desc&page_size=1'
    );
    if (res.ok()) {
      const body = (await res.json()) as { postings?: unknown[] };
      if ((body.postings?.length ?? 0) > 0) return;
    }
    await new Promise(r => setTimeout(r, 2_000));
  }
  throw new Error('no matched postings after activating the target');
}

/** Wait for the jobs table to hold real rows (not the skeleton). */
async function awaitJobRows(page: import('@playwright/test').Page) {
  await expect
    .poll(
      async () =>
        page
          .locator('main table tbody tr')
          .filter({ hasNot: page.locator('.animate-pulse') })
          .count(),
      { timeout: 90_000 }
    )
    .toBeGreaterThan(0);
}

/**
 * Row-select checkboxes are `sr-only` inputs whose hit target is an
 * `aria-hidden` sibling `<span>`; a pointer click on the input itself is
 * intercepted, and Space on the focused input does nothing (see
 * `jobs.select-row.keyboard` below). Clicking the visible proxy is the path a
 * mouse user actually takes, and the only one that works.
 */
async function clickCheckboxProxy(box: import('@playwright/test').Locator) {
  // Locator click, not mouse.click(x,y): the batch bar appearing/disappearing
  // shifts the table, so a bounding box captured a moment earlier lands on the
  // wrong row (or nothing). The locator re-resolves and scrolls at click time.
  await box
    .locator('xpath=following-sibling::span[1]')
    .click({ timeout: 15_000 });
}

async function checkRow(page: import('@playwright/test').Page, nth = 0) {
  const box = page.getByRole('checkbox', { name: /^Select (?!all)/ }).nth(nth);
  await clickCheckboxProxy(box);
  await expect(box).toBeChecked({ timeout: 10_000 });
}

test('deep: dashboard periods, funnel chips, theme, 404s', async ({ page }) => {
  test.setTimeout(300_000);

  await page.goto('/dashboard?view=trends', { waitUntil: 'domcontentloaded' });
  await page
    .waitForLoadState('networkidle', { timeout: 60_000 })
    .catch(() => {});

  for (const [id, name] of [
    ['dash.trends.period.90d', /^90d$/],
    ['dash.trends.period.all', /^All$/],
  ] as const) {
    await timedAction(
      page,
      id,
      'dashboard',
      async () => {
        await page.getByRole('button', { name }).click();
      },
      async () => {
        await page.waitForLoadState('networkidle', { timeout: 60_000 });
      }
    );
  }

  // Regression guard for the 4-bars/2-labels defect found 2026-08-08: every
  // bar in Target Comparison must have a category tick it can be read against.
  await timedAction(
    page,
    'dash.trends.target-comparison.labels',
    'dashboard',
    async () => {
      await page
        .getByText('Target Comparison')
        .first()
        .waitFor({ timeout: 30_000 });
    },
    async () => {
      // Scope to the chart's own SVG. Recharts animates in over ~2s, so wait
      // for a bar to exist before counting — otherwise 0 bars vs 0 ticks makes
      // this assertion vacuously true (it "passed" in 16ms before this fix).
      const svg = page
        .locator('svg.recharts-surface')
        .filter({ has: page.locator('.recharts-bar-rectangle') })
        .last();
      await svg.locator('.recharts-bar-rectangle').first().waitFor({
        timeout: 30_000,
      });
      const bars = await svg.locator('.recharts-bar-rectangle').count();
      const ticks = await svg
        .locator('.recharts-xAxis .recharts-cartesian-axis-tick-value')
        .count();
      expect(bars, 'no Target Comparison bars rendered').toBeGreaterThan(0);
      expect(
        ticks,
        `Target Comparison renders ${bars} bars but only ${ticks} category ` +
          `labels — the unlabelled bars cannot be attributed to a target`
      ).toBeGreaterThanOrEqual(bars);
    }
  );

  // Regression guard for the funnel-chip mismatch: a chip that advertises N
  // jobs must land on a list that actually has N rows. The funnel counts
  // ever-reached stages; /jobs?status= filters current status.
  await timedAction(
    page,
    'dash.funnel.chip.saved',
    'dashboard',
    async () => {
      const chip = page.getByRole('link', { name: /^Saved \(\d+\)$/ });
      await chip.waitFor({ timeout: 30_000 });
      // Capture the number the chip PROMISES before navigating away.
      promisedSaved = Number(
        /\((\d+)\)/.exec((await chip.innerText()) ?? '')?.[1] ?? '-1'
      );
      await chip.click();
      await page.waitForLoadState('networkidle', { timeout: 60_000 });
    },
    async () => {
      expect(promisedSaved, 'could not read the chip count').toBeGreaterThan(
        -1
      );
      const res = await page.request.get(
        '/api/jobs?page_size=100&status=saved'
      );
      const body = (await res.json()) as { postings?: unknown[] };
      const delivered = body.postings?.length ?? 0;
      expect(
        delivered,
        `the funnel chip advertises "Saved (${promisedSaved})" but ` +
          `/jobs?status=saved returns ${delivered} rows — the funnel counts ` +
          `ever-reached stages while the list filters on current status`
      ).toBe(promisedSaved);
    }
  );

  await timedAction(
    page,
    'dash.theme.cycle',
    'dashboard',
    async () => {
      // 3-state cycle (light -> dark -> system). The html class legitimately
      // does NOT change on system->light when the OS is light, so assert the
      // control's own advertised state instead — that always moves.
      const btn = page.getByRole('button', { name: /switch to .* mode/i });
      const before = (await btn.getAttribute('title')) ?? '';
      await btn.click();
      await expect
        .poll(async () => (await btn.getAttribute('title')) ?? '', {
          timeout: 15_000,
        })
        .not.toBe(before);
    },
    async () => {
      // Cycle back so the owner's stored preference is unchanged.
      await page.getByRole('button', { name: /switch to .* mode/i }).click();
      await page.getByRole('button', { name: /switch to .* mode/i }).click();
    }
  );

  await timedAction(
    page,
    'app.404',
    'dashboard',
    async () => {
      await page.goto('/this-route-does-not-exist', {
        waitUntil: 'domcontentloaded',
      });
    },
    async () => {
      await expect(
        page.getByText(/not found|404|doesn’t exist|does not exist/i).first()
      ).toBeVisible({ timeout: 30_000 });
    }
  );

  await timedAction(
    page,
    'jobdetail.404.ghost-id',
    'jobdetail',
    async () => {
      const res = await page.request.get(
        '/api/jobs/00000000-0000-0000-0000-000000000000'
      );
      // #656 gate: a ghost id must 404, never 500 (the .single()/PGRST116 class).
      expect(res.status()).toBe(404);
    },
    async () => {
      /* status assertion above is the success condition */
    }
  );
});

test('deep: jobs filter + sort gaps', async ({ page }) => {
  test.setTimeout(420_000);

  await ensureJobsPopulated(page);
  await page.goto('/jobs', { waitUntil: 'domcontentloaded' });
  await awaitJobRows(page);

  await timedAction(
    page,
    'jobs.sort.title',
    'jobs',
    async () => {
      await page.getByRole('button', { name: 'Sort by Title' }).click();
    },
    async () => {
      await page.waitForLoadState('networkidle', { timeout: 60_000 });
    }
  );

  // One sort click fired THREE /api/jobs requests on 2026-08-08 (one aborted,
  // two completed). Two completed list queries per click is a wasted 0.4–2.2s
  // of API time; this pins the budget at one.
  await timedAction(
    page,
    'jobs.sort.request-fanout',
    'jobs',
    async () => {
      const seen: string[] = [];
      const onReq = (r: import('@playwright/test').Request) => {
        if (r.url().includes('/api/jobs?')) seen.push(r.url());
      };
      page.on('request', onReq);
      await page.getByRole('button', { name: 'Sort by Company' }).click();
      await page.waitForLoadState('networkidle', { timeout: 60_000 });
      page.off('request', onReq);
      // Lower bound too: a click that fired ZERO requests would otherwise
      // satisfy "<= 1" and report a vacuous pass.
      expect(
        seen.length,
        'the sort click issued no /api/jobs request at all'
      ).toBeGreaterThanOrEqual(1);
      expect(
        seen.length,
        `one sort click issued ${seen.length} /api/jobs requests; expected 1`
      ).toBeLessThanOrEqual(1);
    },
    async () => {
      /* assertion above */
    }
  );

  // Score floor. The UI chip says "Score 70+"; every graded row must honour it.
  await timedAction(
    page,
    'jobs.filter.min-score.floor-holds',
    'jobs',
    async () => {
      const res = await page.request.get(
        '/api/jobs?page_size=25&sort=score&order=desc&min_score=70'
      );
      const body = (await res.json()) as {
        postings?: { score: number | null; pending?: boolean }[];
      };
      const offenders = (body.postings ?? []).filter(
        p => !p.pending && typeof p.score === 'number' && p.score < 70
      );
      expect(
        offenders.map(o => o.score),
        'min_score=70 returned non-pending rows whose DISPLAYED score is ' +
          'below 70. Since #665 there is only ONE score: the aged one ' +
          '(scores.recency_score), and the floor judges that same number, so ' +
          'a card under the chip can no longer read lower than the chip. If ' +
          'this fails, the floor and the display have drifted apart again — ' +
          'check that the RPC floors on recency_score AND returns it as ' +
          '`score` (they are the same expression by construction).'
      ).toEqual([]);
    },
    async () => {
      /* assertion above */
    }
  );

  // The floor invariant the RPC fix actually establishes: whatever the display
  // layer then does to the number, no genuinely-graded row may enter a floored
  // list with a raw fit score under the bar. Split out from the guard above so
  // the two defects fail independently — before migration 20260808120000 this
  // returned 858 graded rows under the floor on the owner's target set.
  //
  // #665 repointed this. The shown score IS the aged score now, so
  // "raw_score >= floor" is merely a CONSEQUENCE (decay only ever reduces, so
  // recency >= 70 implies raw >= 70) rather than the thing worth guarding.
  // What is worth guarding is the direction itself: the number on the card must
  // never exceed the raw fit. If it ever does, decay has been applied twice, or
  // inverted, or the weighted-blend branch has stopped decaying — all of which
  // would silently re-open the gap between the chip and the card.
  await timedAction(
    page,
    'jobs.filter.min-score.decay-direction',
    'jobs',
    async () => {
      const res = await page.request.get(
        '/api/jobs?page_size=25&sort=score&order=desc&min_score=70'
      );
      const body = (await res.json()) as {
        postings?: {
          score: number | null;
          raw_score?: number | null;
          pending?: boolean;
        }[];
      };
      const rows = body.postings ?? [];
      // Guard the guard: a page of all-pending rows would satisfy the
      // assertion vacuously, having tested nothing.
      const graded = rows.filter(p => !p.pending);
      expect(
        graded.length,
        'no non-pending rows came back, so the floor assertion below would ' +
          'pass without exercising anything — the fixture or the account has ' +
          'no graded rows above the floor'
      ).toBeGreaterThan(0);
      // ...and guard the guard again: if raw_score were absent the offender
      // filter below could never match, so the assertion would certify the
      // floor while testing nothing.
      expect(
        graded.filter(p => typeof p.raw_score !== 'number'),
        'graded rows came back without a numeric raw_score, so the floor ' +
          'assertion below cannot fail — the API stopped surfacing the ' +
          'undecayed fit score this guard reads'
      ).toHaveLength(0);
      // Decay only ever reduces, so the shown score must sit at or below the
      // raw fit — and, as a consequence of the floor, at or above the bar.
      const inverted = graded.filter(
        p =>
          typeof p.raw_score === 'number' &&
          typeof p.score === 'number' &&
          p.score > p.raw_score
      );
      expect(
        inverted.map(o => `${o.score} > ${o.raw_score}`),
        'a shown score exceeded its own raw fit. Decay multiplies by at most ' +
          '1.0, so this means it was applied twice, inverted, or the ' +
          'weighted-blend branch stopped decaying — the failure modes that ' +
          'would re-open the gap between the "Score 70+" chip and the card.'
      ).toEqual([]);
      const belowBar = graded.filter(
        p => typeof p.raw_score === 'number' && p.raw_score < 70
      );
      expect(
        belowBar.map(o => o.raw_score),
        'min_score=70 admitted graded rows whose RAW fit is below 70. Since ' +
          'the floor judges the aged score and decay only reduces, this is ' +
          'impossible unless recency_score has drifted above score.'
      ).toEqual([]);
    },
    async () => {
      /* assertion above */
    }
  );

  await timedAction(
    page,
    'jobs.filter.country',
    'jobs',
    async () => {
      await page.keyboard.press('Escape');
      const trigger = page.getByRole('button', { name: /any country/i });
      await trigger.click();
      await expect(trigger).toHaveAttribute('aria-expanded', 'true');
      await page.getByRole('menuitem', { name: /^Canada$/ }).click();
      await page.waitForLoadState('networkidle', { timeout: 60_000 });
    },
    async () => {
      // The corpus is US-only by design, so "Canada" must yield zero rows —
      // not a full page of US postings. The filter matches
      // logistics_filters.location_country, a Phase-2 LLM field absent on
      // stage1 rows, and its "absent ⇒ keep" leniency admits the whole corpus.
      const res = await page.request.get('/api/jobs?page_size=25&country=CA');
      const body = (await res.json()) as {
        postings?: { country: string | null }[];
      };
      const nonCa = (body.postings ?? []).filter(
        p => p.country != null && p.country.toUpperCase() !== 'CA'
      );
      expect(
        nonCa.map(p => p.country),
        'country=CA returned postings whose own jobs.country column says ' +
          'otherwise — the filter reads logistics_filters.location_country ' +
          '(graded rows only) and keeps every row where it is absent'
      ).toEqual([]);
    }
  );

  await timedAction(
    page,
    'jobs.filter.location-include',
    'jobs',
    // KNOWN SUITE GAP: typing into the Locations popover and pressing Enter
    // does not commit from automation — no /api/jobs request is issued, so the
    // UI path stays unverified here. The FILTER ITSELF is fine: prod API logs
    // show the real UI sending `only_locations=Remote&exclude_locations=Texas`,
    // and a direct call returns only remote rows. Asserting at the API layer
    // until the popover's real commit gesture is identified. (An earlier
    // version of this guard read a shared "last seen query" variable, raced
    // with the next action, and reported a WORKING filter as inert — the API
    // logs are what caught it.)
    async () => {
      await page.keyboard.press('Escape');
      const res = await page.request.get(
        '/api/jobs?page_size=25&only_locations=Remote'
      );
      locationQuery = res.url();
      const body = (await res.json()) as { postings?: { location?: string }[] };
      const offTarget = (body.postings ?? []).filter(
        x => !/remote/i.test(x.location ?? '')
      );
      expect(
        offTarget.map(x => x.location),
        'only_locations=Remote returned rows whose location is not remote'
      ).toEqual([]);
    },
    async () => {
      // NOTE: unlike score / salary / country / status / search, the Locations
      // filter does NOT write to the URL, so it can't be shared or restored on
      // reload. Assert the outgoing query instead — "a row is visible" alone
      // passes even when the interaction did nothing at all.
      expect(locationQuery, 'the only_locations param was not sent').toMatch(
        /only_locations=/
      );
      await expect(page.locator('main table tbody tr').first()).toBeVisible({
        timeout: 30_000,
      });
    }
  );

  await timedAction(
    page,
    'jobs.filter.location-exclude',
    'jobs',
    // Same known suite gap as location-include above. The filter is a
    // documented case-insensitive SUBSTRING match on the location string, so
    // "Austin, TX" is NOT excluded by "Texas" — only the literal substring is.
    async () => {
      const res = await page.request.get(
        '/api/jobs?page_size=50&exclude_locations=Texas'
      );
      locationQuery = res.url();
      const body = (await res.json()) as { postings?: { location?: string }[] };
      const leaked = (body.postings ?? []).filter(x =>
        /texas/i.test(x.location ?? '')
      );
      expect(
        leaked.map(x => x.location),
        'exclude_locations=Texas returned rows whose location contains "Texas"'
      ).toEqual([]);
      await page.keyboard.press('Escape');
    },
    async () => {
      expect(locationQuery, 'the exclude_locations param was not sent').toMatch(
        /exclude_locations=/
      );
      await expect(page.locator('main table tbody tr').first()).toBeVisible({
        timeout: 30_000,
      });
    }
  );

  // There is no "Clear all" control (the earlier sweep's selector was invented
  // and burned a 45s timeout). Clearing is per-chip via its ✕.
  await timedAction(
    page,
    'jobs.filter.clear',
    'jobs',
    // "Clear all" lives INSIDE the active-filters region, so it only exists
    // while at least one chip is up — the earlier sweep's 45s timeout was the
    // action running where no filter was applied, not a missing control.
    // Establish chips from the URL first, then clear.
    async () => {
      await page.goto('/jobs?score=70&min_salary=150000', {
        waitUntil: 'domcontentloaded',
      });
      const chips = page.locator('[aria-label*="ctive filter" i]');
      await chips.waitFor({ timeout: 60_000 });
      await chips.getByRole('button', { name: /^clear all$/i }).click();
    },
    async () => {
      await page.waitForLoadState('networkidle', { timeout: 60_000 });
      await expect(page.locator('[aria-label*="ctive filter" i]')).toBeHidden({
        timeout: 20_000,
      });
    }
  );

  // 5 keystrokes must collapse into ONE request.
  await timedAction(
    page,
    'jobs.search.debounce',
    'jobs',
    async () => {
      const seen: string[] = [];
      const onReq = (r: import('@playwright/test').Request) => {
        if (r.url().includes('/api/jobs?')) seen.push(r.url());
      };
      page.on('request', onReq);
      await page
        .getByPlaceholder(/search by title/i)
        .pressSequentially('react', { delay: 60 });
      await page.waitForLoadState('networkidle', { timeout: 60_000 });
      page.off('request', onReq);
      expect(
        seen.length,
        `5 keystrokes issued ${seen.length} /api/jobs requests`
      ).toBeLessThanOrEqual(2);
    },
    async () => {
      /* assertion above */
    }
  );
});

test('deep: jobs panel writes, batch bar, manual add + delete', async ({
  page,
}) => {
  test.setTimeout(600_000);

  await ensureJobsPopulated(page);
  await page.goto('/jobs', { waitUntil: 'domcontentloaded' });
  await awaitJobRows(page);

  await timedAction(
    page,
    'jobs.select-all',
    'jobs',
    async () => {
      const all = page.getByRole('checkbox', {
        name: /select all on this page/i,
      });
      await all.focus();
      await page.keyboard.press('Space');
      await expect(all).toBeChecked();
    },
    async () => {
      await expect(page.getByText(/\d+ selected/i).first()).toBeVisible({
        timeout: 15_000,
      });
    }
  );

  // WCAG 2.1.1. The header "Select all on this page" checkbox toggles on Space
  // (asserted above), but the per-row "Select <job>" checkbox in the same table
  // does not — reproduced deterministically on 2026-08-08. A keyboard user can
  // therefore select every row or none, never one: batch tailor / export / delete
  // become all-or-nothing.
  await timedAction(
    page,
    'jobs.select-row.keyboard',
    'jobs',
    async () => {
      // Start from a clean slate so the assertion reads a real transition.
      const all = page.getByRole('checkbox', {
        name: /select all on this page/i,
      });
      if (await all.isChecked()) {
        await all.focus();
        await page.keyboard.press('Space');
      }
      const row = page
        .getByRole('checkbox', { name: /^Select (?!all)/ })
        .first();
      await expect(row).not.toBeChecked();
      await row.focus();
      await expect(row).toBeFocused();
      await page.keyboard.press('Space');
    },
    async () => {
      await expect(
        page.getByRole('checkbox', { name: /^Select (?!all)/ }).first(),
        'pressing Space on a focused row-select checkbox did not toggle it — ' +
          'the click handler lives on the aria-hidden proxy <span>, not the ' +
          'sr-only input that actually receives focus'
      ).toBeChecked({ timeout: 5_000 });
    }
  );

  await timedAction(
    page,
    'jobs.batch.export-zip',
    'jobs',
    async () => {
      const res = await page.request.post('/api/jobs/tailor/export-zip', {
        data: { job_posting_ids: [] },
      });
      // Empty selection must be a clean 4xx, not a 500 or a 0-byte zip.
      expect(res.status()).toBeGreaterThanOrEqual(400);
      expect(res.status()).toBeLessThan(500);
    },
    async () => {
      /* assertion above */
    }
  );

  // Deselect all, then batch-tailor exactly one row (bounded LLM spend).
  // Idempotent on purpose: the keyboard guard above may already have cleared
  // the selection, and an unconditional toggle here re-selected all 20 rows —
  // so the subsequent row click was UNchecking rather than checking.
  const selectAll = page.getByRole('checkbox', {
    name: /select all on this page/i,
  });
  if (await selectAll.isChecked()) {
    await selectAll.focus();
    await page.keyboard.press('Space');
    await expect(selectAll).not.toBeChecked({ timeout: 10_000 });
  }
  await checkRow(page, 0);

  await timedAction(
    page,
    'jobs.batch.tailor',
    'jobs',
    async () => {
      const btn = page
        .getByRole('button', { name: /tailor|generate/i })
        .first();
      await btn.click();
    },
    async () => {
      // The batch runner is 202 + poll; a queued/È running state is success.
      await expect(
        page
          .getByText(/queued|generating|in progress|tailoring|drafted/i)
          .first()
      ).toBeVisible({ timeout: 120_000 });
    },
    { deadlineMs: 180_000 }
  );

  // ---- panel-level writes on a single row --------------------------------
  await ensureJobsPopulated(page);
  await page.goto('/jobs', { waitUntil: 'domcontentloaded' });
  await awaitJobRows(page);
  await page.locator('tbody tr td:nth-child(5)').first().click();
  await page.waitForTimeout(2_500);

  await timedAction(
    page,
    'jobs.panel.target-membership',
    'jobs',
    async () => {
      const res = await page.request.get('/api/jobs/target-membership');
      expect([200, 400, 422]).toContain(res.status());
    },
    async () => {
      /* assertion above */
    }
  );

  await timedAction(
    page,
    'jobs.panel.add-to-target',
    'jobs',
    async () => {
      const rows = await (
        await page.request.get('/api/jobs?page_size=1&sort=score&order=desc')
      ).json();
      const id = rows.postings?.[0]?.id as string | undefined;
      if (!id) throw new Error('no posting to add');
      const res = await page.request.post(`/api/jobs/${id}/add-to-target`, {
        data: { target_id: REAL_TARGET_ID },
      });
      expect(res.status()).toBeLessThan(500);
    },
    async () => {
      /* assertion above */
    }
  );

  await timedAction(
    page,
    'jobs.panel.feedback.vote',
    'jobs',
    async () => {
      await page
        .getByRole('button', { name: /great match/i })
        .first()
        .click();
    },
    async () => {
      await expect(
        page.getByRole('button', { name: /great match/i }).first()
      ).toHaveAttribute('aria-pressed', 'true', { timeout: 20_000 });
    }
  );
  // Restore: un-vote so the owner's learning signal is unchanged.
  await page
    .getByRole('button', { name: /great match/i })
    .first()
    .click()
    .catch(() => {});

  // ---- manual add → delete round trip (own data only) --------------------
  await timedAction(
    page,
    'jobs.add-manual-url',
    'jobs',
    async () => {
      const res = await page.request.post('/api/jobs/manual', {
        data: { url: MANUAL_ADD_URL },
      });
      expect(res.status()).toBeLessThan(500);
      const body = (await res.json()) as {
        id?: string;
        posting?: { id: string };
      };
      manualJobId = body.id ?? body.posting?.id ?? null;
    },
    async () => {
      /* assertion above */
    },
    { deadlineMs: 180_000 }
  );

  await timedAction(
    page,
    'jobs.delete-posting',
    'jobs',
    async () => {
      if (!manualJobId) throw new Error('manual add produced no id to delete');
      const res = await page.request.delete(`/api/jobs/${manualJobId}`);
      expect(res.status()).toBeLessThan(500);
    },
    async () => {
      if (!manualJobId) return;
      const res = await page.request.get(`/api/jobs/${manualJobId}`);
      expect(res.status()).toBe(404);
    }
  );

  await timedAction(
    page,
    'jobdetail.status.change',
    'jobdetail',
    async () => {
      const rows = await (
        await page.request.get('/api/jobs?page_size=1&status=resume_draft')
      ).json();
      draftJobId = rows.postings?.[0]?.id ?? null;
      if (!draftJobId) throw new Error('no resume_draft posting');
      const res = await page.request.patch(`/api/jobs/${draftJobId}/status`, {
        data: { status: 'saved' },
      });
      expect(res.ok()).toBe(true);
    },
    async () => {
      // Restore the original status immediately.
      if (!draftJobId) return;
      const res = await page.request.patch(`/api/jobs/${draftJobId}/status`, {
        data: { status: 'resume_draft' },
      });
      expect(res.ok()).toBe(true);
    }
  );
});

test('deep: cover-letter parity + resume ats/readapt', async ({ page }) => {
  test.setTimeout(900_000);

  const rows = await (
    await page.request.get('/api/jobs?page_size=1&status=resume_draft')
  ).json();
  const jobId = rows.postings?.[0]?.id as string | undefined;
  test.skip(!jobId, 'no resume_draft posting to exercise the tailor surface');
  if (!jobId) return;

  await page.goto(`/jobs/${jobId}/resume`, { waitUntil: 'domcontentloaded' });
  await page.getByRole('heading', { name: /review tailored resume/i }).waitFor({
    timeout: 90_000,
  });

  // The BFF wraps the run in `record` — reading `.id` off the envelope
  // returned undefined and skipped four tailor actions on the first run.
  const tailorId = await page.request
    .get(`/api/jobs/tailor/by-job/${jobId}`)
    .then(async r => {
      const j = (await r.json()) as { record?: { id?: string }; id?: string };
      return j.record?.id ?? j.id ?? null;
    })
    .catch(() => null);

  await timedAction(
    page,
    'resume.ats-recheck',
    'tailor',
    async () => {
      if (!tailorId) throw new Error('no tailor run id for this job');
      const res = await page.request.post(
        `/api/jobs/tailor/${tailorId}/ats-recheck`
      );
      expect(res.status()).toBeLessThan(500);
    },
    async () => {
      /* assertion above */
    },
    { deadlineMs: 180_000 }
  );

  await timedAction(
    page,
    'resume.flagged-draft.render',
    'tailor',
    async () => {
      await page
        .getByRole('heading', { name: /review tailored resume/i })
        .waitFor({ timeout: 60_000 });
    },
    async () => {
      // #656: a flagged draft must still render its body, not an inert page.
      const body = await page.locator('main').innerText();
      expect(body.length).toBeGreaterThan(400);
    }
  );

  await timedAction(
    page,
    'resume.readapt.llm',
    'tailor',
    async () => {
      if (!tailorId) throw new Error('no tailor run id');
      const res = await page.request.post('/api/jobs/tailor/resume', {
        data: { job_posting_id: jobId, force: true },
      });
      expect(res.status()).toBeLessThan(500);
    },
    async () => {
      /* 202 + background runner; completion is covered by resume.generate.llm */
    },
    { deadlineMs: 300_000 }
  );

  // ---- cover letter: the same six actions the resume page already has ----
  await page.goto(`/jobs/${jobId}/cover-letter`, {
    waitUntil: 'domcontentloaded',
  });
  await page.getByRole('heading', { name: /review cover letter/i }).waitFor({
    timeout: 90_000,
  });

  await timedAction(
    page,
    'cover.flagged-draft.render',
    'tailor',
    async () => {
      await page.waitForLoadState('networkidle', { timeout: 60_000 });
    },
    async () => {
      const body = await page.locator('main').innerText();
      expect(
        body.length,
        'cover-letter review page rendered an inert shell (#656 regression)'
      ).toBeGreaterThan(400);
    }
  );

  await timedAction(
    page,
    'cover.versions.open',
    'tailor',
    async () => {
      await page.getByRole('button', { name: /version history/i }).click();
    },
    async () => {
      await page.waitForLoadState('networkidle', { timeout: 45_000 });
    }
  );

  await timedAction(
    page,
    'cover.edit.autosave',
    'tailor',
    async () => {
      const editor = page.locator('[contenteditable="true"]').first();
      await editor.click();
      await page.keyboard.press('End');
      await page.keyboard.type(' ');
      await page.keyboard.press('Backspace');
    },
    async () => {
      await page.waitForLoadState('networkidle', { timeout: 45_000 });
    }
  );

  const coverId = await page.request
    .get(`/api/jobs/tailor/by-job/${jobId}/cover-letter`)
    .then(async r => {
      const j = (await r.json()) as { record?: { id?: string }; id?: string };
      return j.record?.id ?? j.id ?? null;
    })
    .catch(() => null);

  await timedAction(
    page,
    'cover.checkpoint',
    'tailor',
    async () => {
      if (!coverId) throw new Error('no cover-letter run id');
      const res = await page.request.post(
        `/api/jobs/tailor/${coverId}/checkpoint`
      );
      expect(res.status()).toBeLessThan(500);
    },
    async () => {
      /* assertion above */
    }
  );

  await timedAction(
    page,
    'cover.approve',
    'tailor',
    async () => {
      if (!coverId) throw new Error('no cover-letter run id');
      const res = await page.request.post(
        `/api/jobs/tailor/${coverId}/approve`
      );
      expect(res.status()).toBeLessThan(500);
    },
    async () => {
      /* assertion above */
    }
  );

  await timedAction(
    page,
    'cover.unapprove',
    'tailor',
    async () => {
      if (!coverId) throw new Error('no cover-letter run id');
      const res = await page.request.post(
        `/api/jobs/tailor/${coverId}/unapprove`
      );
      expect(res.status()).toBeLessThan(500);
    },
    async () => {
      /* restored */
    }
  );
});

test('deep: targets CRUD on a throwaway target', async ({ page }) => {
  test.setTimeout(900_000);

  await timedAction(
    page,
    'targets.search',
    'targets',
    async () => {
      const res = await page.request.get('/api/targets/search?q=engineer');
      expect(res.ok()).toBe(true);
    },
    async () => {
      /* assertion above */
    }
  );

  await timedAction(
    page,
    'targets.suggest.list',
    'targets',
    async () => {
      const res = await page.request.get('/api/targets/suggest');
      expect(res.status()).toBeLessThan(500);
    },
    async () => {
      /* assertion above */
    },
    { deadlineMs: 120_000 }
  );

  await timedAction(
    page,
    'targets.suggest.lateral',
    'targets',
    async () => {
      const res = await page.request.get('/api/targets/suggest-lateral');
      expect(res.status()).toBeLessThan(500);
    },
    async () => {
      /* assertion above */
    },
    { deadlineMs: 180_000 }
  );

  await timedAction(
    page,
    'targets.suggest.from-query',
    'targets',
    async () => {
      const res = await page.request.post('/api/targets/suggest-from-query', {
        data: { query: 'staff frontend engineer' },
      });
      expect(res.status()).toBeLessThan(500);
    },
    async () => {
      /* assertion above */
    },
    { deadlineMs: 180_000 }
  );

  await timedAction(
    page,
    'targets.create.from-label',
    'targets',
    async () => {
      const res = await page.request.post('/api/targets/from-manual', {
        data: { label: SCRATCH_TARGET_LABEL },
      });
      expect(res.status()).toBeLessThan(500);
      const body = (await res.json()) as {
        target_id?: string;
        id?: string;
        user_target?: { id: string; target_id: string };
      };
      scratchTargetId =
        body.target_id ?? body.user_target?.target_id ?? body.id ?? null;
      scratchUserTargetId = body.user_target?.id ?? null;
      expect(scratchTargetId, 'from-manual returned no target id').toBeTruthy();
    },
    async () => {
      /* assertion above */
    },
    { deadlineMs: 300_000 }
  );

  await timedAction(
    page,
    'targets.create.from-url',
    'targets',
    async () => {
      const res = await page.request.post('/api/targets/from-url', {
        data: { url: MANUAL_ADD_URL },
      });
      // Whatever it does, it must not 5xx.
      expect(res.status()).toBeLessThan(500);
    },
    async () => {
      /* assertion above */
    },
    { deadlineMs: 300_000 }
  );

  await timedAction(
    page,
    'targets.link',
    'targets',
    async () => {
      if (!scratchTargetId) throw new Error('no scratch target');
      const res = await page.request.post(
        `/api/targets/${scratchTargetId}/link`
      );
      expect(res.status()).toBeLessThan(500);
    },
    async () => {
      /* assertion above */
    }
  );

  // ---- detail-page UI on the scratch target ------------------------------
  if (scratchTargetId) {
    await page.goto(`/targets/${scratchTargetId}`, {
      waitUntil: 'domcontentloaded',
    });
    await page
      .waitForLoadState('networkidle', { timeout: 60_000 })
      .catch(() => {});

    await timedAction(
      page,
      'targets.detail.axis-weights.adjust',
      'targets',
      async () => {
        const slider = page.getByRole('slider', { name: /title fit weight/i });
        await slider.focus();
        await page.keyboard.press('ArrowRight');
        // Save stays disabled until the slider actually moves — clicking a
        // disabled button just burns the action timeout.
        const save = page.locator('button[name="axis-weights-save"]');
        await expect(save).toBeEnabled({ timeout: 15_000 });
        await save.click();
      },
      async () => {
        await page.waitForLoadState('networkidle', { timeout: 45_000 });
      }
    );

    await timedAction(
      page,
      'targets.detail.axis-weights.undo',
      'targets',
      async () => {
        const undo = page.locator('button[name="axis-weights-undo"]');
        await expect(undo).toBeEnabled({ timeout: 20_000 });
        await undo.click();
      },
      async () => {
        await page.waitForLoadState('networkidle', { timeout: 45_000 });
      }
    );

    await timedAction(
      page,
      'targets.detail.preferences.save',
      'targets',
      async () => {
        await page.getByRole('tab', { name: /preferences/i }).click();
        await page
          .getByRole('spinbutton', { name: /minimum fit score/i })
          .fill('42');
        // Two "Save" buttons live on this tab (preferences + notification
        // thresholds) — a name-only locator is a strict-mode violation.
        await page.locator('button[name="target-preferences-save"]').click();
      },
      async () => {
        await page.waitForLoadState('networkidle', { timeout: 45_000 });
      }
    );

    await timedAction(
      page,
      'targets.detail.notification-thresholds',
      'targets',
      async () => {
        await page
          .getByRole('spinbutton', { name: /email alerts score threshold/i })
          .fill('80');
        await page
          .locator('button[name="notification-thresholds-save"]')
          .click();
      },
      async () => {
        await page.waitForLoadState('networkidle', { timeout: 45_000 });
      }
    );

    await timedAction(
      page,
      'targets.detail.reference-jds.vote',
      'targets',
      async () => {
        await page.getByRole('tab', { name: /reference jds/i }).click();
        await page.waitForTimeout(1_500);
        const up = page.getByRole('button', { name: /upvote reference jd/i });
        if ((await up.count()) === 0) {
          throw new Error('scratch target has no reference JD to vote on');
        }
        await up.first().click();
      },
      async () => {
        await page.waitForLoadState('networkidle', { timeout: 45_000 });
      }
    );

    await timedAction(
      page,
      'targets.learn.run',
      'targets',
      async () => {
        await page.getByRole('tab', { name: /learning/i }).click();
        await page.getByRole('button', { name: /check for updates/i }).click();
      },
      async () => {
        await page.waitForLoadState('networkidle', { timeout: 120_000 });
      },
      { deadlineMs: 240_000 }
    );

    await timedAction(
      page,
      'targets.learn.reject',
      'targets',
      async () => {
        const log = await (
          await page.request.get(`/api/targets/${scratchTargetId}/learning-log`)
        ).json();
        const runId = (log.runs ?? log.entries ?? [])[0]?.id as
          string | undefined;
        if (!runId) throw new Error('learning run produced no suggestion');
        const res = await page.request.post(
          `/api/targets/${scratchTargetId}/learn/${runId}/reject`
        );
        expect(res.status()).toBeLessThan(500);
      },
      async () => {
        /* assertion above */
      }
    );
  }

  await timedAction(
    page,
    'targets.delete',
    'targets',
    async () => {
      // DELETE keys on target_id — passing the user_target id 404s (verified
      // 2026-08-08, which is why the first run reported "scratch target
      // survived deletion"). Keep the user_target id only as a fallback.
      const id = scratchTargetId ?? scratchUserTargetId;
      if (!id) throw new Error('no scratch target to delete');
      const res = await page.request.delete(`/api/targets/${id}`);
      expect(res.status(), 'DELETE /api/targets/<target_id> failed').toBe(200);
    },
    async () => {
      const body = await (await page.request.get('/api/targets/mine')).json();
      const labels = JSON.stringify(body);
      expect(
        labels.includes(SCRATCH_TARGET_LABEL),
        'scratch target survived deletion'
      ).toBe(false);
    }
  );
});

test('deep: profile, settings, billing sessions, onboarding round-trip', async ({
  page,
}) => {
  test.setTimeout(900_000);

  await page.goto('/profile', { waitUntil: 'domcontentloaded' });
  await page
    .waitForLoadState('networkidle', { timeout: 60_000 })
    .catch(() => {});

  await timedAction(
    page,
    'profile.identity.save',
    'profile',
    async () => {
      const current = await (
        await page.request.get('/api/profile/identity')
      ).json();
      const res = await page.request.put('/api/profile/identity', {
        data: current,
      });
      expect(res.status()).toBeLessThan(500);
    },
    async () => {
      /* idempotent write-back: no field value changes */
    }
  );

  await timedAction(
    page,
    'profile.experience.conversation.probe',
    'profile',
    async () => {
      const res = await page.request.get(
        '/api/career/experience/conversation/next-probe'
      );
      expect(res.status()).toBeLessThan(500);
    },
    async () => {
      /* assertion above */
    },
    { deadlineMs: 180_000 }
  );

  await timedAction(
    page,
    'profile.export.download',
    'profile',
    async () => {
      const res = await page.request.get('/api/profile/export');
      expect(res.status()).toBeLessThan(500);
      if (res.ok()) {
        expect((await res.body()).byteLength).toBeGreaterThan(0);
      }
    },
    async () => {
      /* assertion above */
    },
    { deadlineMs: 180_000 }
  );

  await timedAction(
    page,
    'profile.keys.load',
    'profile',
    async () => {
      const res = await page.request.get('/api/profile/keys');
      expect(res.ok()).toBe(true);
    },
    async () => {
      /* assertion above */
    }
  );

  await timedAction(
    page,
    'profile.keys.add-remove',
    'profile',
    async () => {
      // A syntactically valid but non-functional key: proves the round trip
      // without ever storing a live credential.
      const res = await page.request.put('/api/profile/keys', {
        data: {
          provider: 'openrouter',
          api_key: 'sk-or-v1-e2e-coverage-not-a-real-key',
        },
      });
      expect(res.status()).toBeLessThan(500);
    },
    async () => {
      const del = await page.request.delete('/api/profile/keys/openrouter');
      expect(del.status()).toBeLessThan(500);
    }
  );

  await timedAction(
    page,
    'profile.upload-resume.validate',
    'profile',
    async () => {
      // Validation gate only — a real upload would replace the owner's
      // experience source material with no restore path.
      const res = await page.request.post(
        '/api/career/experience/upload-resume',
        {
          multipart: {
            file: {
              name: 'not-a-resume.txt',
              mimeType: 'text/plain',
              buffer: Buffer.from('e2e coverage probe'),
            },
          },
        }
      );
      expect(
        res.status(),
        'an unsupported upload must be rejected, not 5xx'
      ).toBeLessThan(500);
    },
    async () => {
      /* assertion above */
    },
    { deadlineMs: 120_000 }
  );

  await timedAction(
    page,
    'profile.experience.derive',
    'profile',
    async () => {
      const res = await page.request.post('/api/career/experience/derive');
      expect(res.status()).toBeLessThan(500);
    },
    async () => {
      /* 202 + background; the optimized doc load action covers the result */
    },
    { deadlineMs: 420_000 }
  );

  // ---- settings ----------------------------------------------------------
  await page.goto('/settings', { waitUntil: 'domcontentloaded' });
  await page
    .waitForLoadState('networkidle', { timeout: 60_000 })
    .catch(() => {});

  await timedAction(
    page,
    'settings.notifications.save',
    'settings',
    async () => {
      const current = await (
        await page.request.get('/api/profile/notifications')
      ).json();
      const res = await page.request.put('/api/profile/notifications', {
        data: current,
      });
      expect(res.status()).toBeLessThan(500);
    },
    async () => {
      /* idempotent write-back */
    }
  );

  await timedAction(
    page,
    'settings.billing.checkout.session',
    'settings',
    async () => {
      // Creates a Stripe Checkout session. NOTHING is submitted — no card
      // details are entered and the returned URL is never navigated to.
      const res = await page.request.post('/api/billing/checkout-session', {
        data: { plan: 'starter' },
      });
      expect(res.status()).toBeLessThan(500);
    },
    async () => {
      /* assertion above */
    },
    { deadlineMs: 120_000 }
  );

  await timedAction(
    page,
    'settings.billing.portal.session',
    'settings',
    async () => {
      const res = await page.request.post('/api/billing/portal-session');
      expect(res.status()).toBeLessThan(500);
    },
    async () => {
      /* assertion above */
    },
    { deadlineMs: 120_000 }
  );

  await timedAction(
    page,
    'profile.account.delete-dialog',
    'profile',
    async () => {
      await page.getByRole('tab', { name: /account/i }).click();
      await page.getByRole('button', { name: /delete my account/i }).click();
    },
    async () => {
      // The confirmation gate must exist; the sweep stops here by design.
      await expect(
        page
          .getByRole('dialog')
          .or(page.getByText(/permanent|cannot be undone|are you sure/i))
          .first()
      ).toBeVisible({ timeout: 20_000 });
      await page.keyboard.press('Escape');
    }
  );

  // ---- onboarding round trip --------------------------------------------
  await timedAction(
    page,
    'onboarding.reset',
    'onboarding',
    async () => {
      const res = await page.request.post('/api/profile/onboarding/reset');
      expect(res.status()).toBeLessThan(500);
    },
    async () => {
      /* assertion above */
    }
  );

  await timedAction(
    page,
    'onboarding.wizard.steps',
    'onboarding',
    async () => {
      await page.goto('/onboarding', { waitUntil: 'domcontentloaded' });
      await page.waitForLoadState('networkidle', { timeout: 60_000 });
    },
    async () => {
      await expect(page.locator('main')).toBeVisible({ timeout: 30_000 });
      const res = await page.request.post('/api/profile/onboarding/step', {
        data: { step: 1 },
      });
      expect(res.status()).toBeLessThan(500);
    },
    { deadlineMs: 180_000 }
  );

  await timedAction(
    page,
    'onboarding.complete-restore',
    'onboarding',
    async () => {
      const res = await page.request.post('/api/profile/onboarding/complete');
      expect(res.status()).toBeLessThan(500);
    },
    async () => {
      await page.goto('/onboarding', { waitUntil: 'domcontentloaded' });
      await expect(page).toHaveURL(/dashboard|jobs/, { timeout: 45_000 });
    }
  );
});

test('deep: BFF gates', async ({ page }) => {
  test.setTimeout(300_000);

  // Audit #29 privilege-escalation guard: a plain authenticated session must
  // NOT be able to trigger the global, all-tenant, cost-bearing poll.
  await timedAction(
    page,
    'bff.jobs.poll',
    'bff',
    async () => {
      const res = await page.request.post('/api/jobs/poll');
      expect(
        res.status(),
        'an authenticated session reached the cron-only global poll trigger'
      ).toBe(403);
    },
    async () => {
      /* assertion above */
    }
  );

  await timedAction(
    page,
    'bff.email.target-paused',
    'bff',
    async () => {
      // Validation gate only — a valid payload would send real email.
      const res = await page.request.post('/api/email/target-paused', {
        data: {},
      });
      expect(res.status()).toBeGreaterThanOrEqual(400);
      expect(res.status()).toBeLessThan(500);
    },
    async () => {
      /* assertion above */
    }
  );

  await timedAction(
    page,
    'bff.waitlist',
    'bff',
    async () => {
      // RFC 2606 reserved domain — no real person is ever signed up.
      const bad = await page.request.post('/api/waitlist', {
        data: { email: 'not-an-email' },
      });
      expect(bad.status()).toBeGreaterThanOrEqual(400);
      const ok = await page.request.post('/api/waitlist', {
        data: { email: 'e2e-coverage@example.com' },
      });
      expect(ok.status()).toBeLessThan(500);
    },
    async () => {
      /* assertions above */
    }
  );

  await timedAction(
    page,
    'bff.search-events',
    'bff',
    async () => {
      // The BFF is fire-and-forget: it 204s no matter what the API says, so a
      // `< 500` assertion here can never fail. The API logs showed the previous
      // payload rejected 422 UPSTREAM while this row stayed green. Send the
      // real contract (app/search/searchEvents.ts) so the beacon lands.
      const listing = (await (
        await page.request.get('/api/jobs?page_size=1&sort=score&order=desc')
      ).json()) as { postings?: { id?: string }[] };
      const res = await page.request.post('/api/search-events', {
        data: {
          event_type: 'card_open',
          surface: 'authed',
          job_posting_id: listing.postings?.[0]?.id,
        },
      });
      expect(res.status()).toBe(204);
    },
    async () => {
      /* assertion above */
    }
  );
});

test('deep: teardown — restore target activation state', async ({ page }) => {
  test.setTimeout(120_000);
  await timedAction(
    page,
    'targets.deactivate-restore',
    'targets',
    async () => {
      // Restore, don't blanket-deactivate. Deactivating a target that was
      // already active before the sweep would (a) change state the sweep
      // doesn't own and (b) abort whatever poll fan-out is in flight. If the
      // owner had it active, leave it active.
      if (capturedOriginalState && !wasActiveBeforeSweep) {
        const res = await page.request.post(
          `/api/targets/${REAL_TARGET_ID}/deactivate`
        );
        expect(res.status()).toBeLessThan(500);
      }
    },
    async () => {
      if (capturedOriginalState) {
        expect(await targetIsActive(page)).toBe(wasActiveBeforeSweep);
      }
    }
  );
});
