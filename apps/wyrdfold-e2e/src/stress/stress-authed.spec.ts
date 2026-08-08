/* eslint-disable playwright/no-networkidle, playwright/no-wait-for-timeout, playwright/no-skipped-test, playwright/expect-expect --
 * Timing harness, not a functional spec: 'networkidle' IS the measurement
 * (action -> network settled), fixed settle waits bracket trace capture,
 * and per-action pass/fail lives in the JSONL ledger + coverage gate
 * rather than per-test expect()s. */
import { expect, test } from '@playwright/test';
import { timedAction } from './timing';

/**
 * Authed full-app sweep. Serial within one worker; state-restore
 * discipline: the target's activation is CAPTURED at setup and put back
 * exactly as found, reversible status flips are reverted, approve is
 * followed by unapprove. LLM spend is bounded to one analysis + one resume
 * + one cover letter (~$0.15), all on ONE job so the artifacts land where
 * the owner can find (or delete) them.
 */

const TARGET_ID = '012202b0-e6bd-4617-b9cb-226648e8218e';

test.describe.configure({ mode: 'serial' });

let analysisJobId: string | null = null;

/**
 * The target's activation state BEFORE this sweep touched anything.
 *
 * This used to be a hardcoded assumption — the teardown deactivated
 * unconditionally, on a comment that said "the owner had none active". That
 * assumption went stale: the owner's target 012202b0 is normally left ACTIVE,
 * so every full sweep silently switched their pipeline off and left /jobs
 * rendering an empty list until somebody noticed. (Observed 2026-08-08: after
 * a sweep, `/api/jobs` returned 0 postings for every filter.)
 *
 * `stress-authed-deep.spec.ts` already does capture-and-restore; it just could
 * not help, because it faithfully restored the *already-wrong* state this file
 * left behind. Ask the app what the state is, and put that back.
 */
let wasActiveBeforeSweep: boolean | null = null;

async function readIsActive(
  page: import('@playwright/test').Page
): Promise<boolean> {
  const res = await page.request.get(`/api/targets/${TARGET_ID}/user-target`);
  if (!res.ok()) return false;
  const body = (await res.json()) as {
    is_active?: boolean;
    user_target?: { is_active?: boolean };
  };
  return Boolean(body.is_active ?? body.user_target?.is_active);
}

test('setup: activate target for jobs coverage', async ({ page }) => {
  // Must exceed the 60s readiness poll below plus the activate call itself.
  test.setTimeout(180_000);
  await timedAction(
    page,
    'targets.activate',
    'targets',
    async () => {
      // Only activate when it isn't already. Activation spawns a poll fan-out,
      // and a redundant one just races the previous cycle — the sweep logged
      // ten `deactivated mid-fan-out — aborting remaining sources` lines in
      // prod on 2026-08-08 by cycling this target ~6 times per run.
      //
      // The same read doubles as the restore baseline (see
      // ``wasActiveBeforeSweep``): capture BEFORE mutating, or the teardown has
      // nothing truthful to put back.
      const active = await readIsActive(page);
      if (wasActiveBeforeSweep === null) wasActiveBeforeSweep = active;
      if (!active) {
        const res = await page.request.post(
          `/api/targets/${TARGET_ID}/activate`
        );
        if (!res.ok()) throw new Error(`activate ${res.status()}`);
      }
    },
    async () => {
      const res = await page.request.get(
        `/api/targets/${TARGET_ID}/user-target`
      );
      if (!res.ok()) throw new Error(`user-target ${res.status()}`);
    }
  );

  // Activation is ASYNC: the POST returns as soon as the pipeline is
  // spawned, so the dashboard journey used to race it and find an empty
  // state (dash.top-match.click-through burned 45s waiting for a job link
  // that did not exist yet).
  //
  // Gate on exactly what the dashboard renders — the first page of
  // score-sorted new matches — NOT on activation_status: the pipeline sits
  // in 'polling' for many minutes while it fans out over the whole source
  // catalog, long after matches are servable (measured: still 'polling'
  // 6min in with 11,966 scored live rows). Waiting for 'ready' timed the
  // setup out and skipped the entire sweep.
  const deadline = Date.now() + 60_000;
  let matches = 0;
  while (Date.now() < deadline) {
    const res = await page.request.get(
      '/api/jobs?status=new&sort=score&order=desc&page_size=1'
    );
    if (res.ok()) {
      const body = (await res.json()) as { postings?: unknown[] };
      matches = body.postings?.length ?? 0;
      if (matches > 0) break;
    }
    await new Promise(r => setTimeout(r, 2_000));
  }

  console.log(`[stress] activation settled: first-page matches=${matches}`);
});

test('dashboard journey', async ({ page }) => {
  test.setTimeout(300_000);

  await timedAction(
    page,
    'dash.today.load',
    'dashboard',
    async () => {
      await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    },
    async () => {
      await expect(page.getByText(/new matches/i).first()).toBeVisible({
        timeout: 45_000,
      });
    }
  );

  await timedAction(
    page,
    'dash.toggle.trends',
    'dashboard',
    async () => {
      await page.getByRole('button', { name: /^trends$/i }).click();
    },
    async () => {
      // Optimistic toggle: selected state flips immediately (#615).
      await expect(page.getByRole('button', { name: /^trends$/i })).toBeVisible(
        { timeout: 10_000 }
      );
    }
  );

  await timedAction(
    page,
    'dash.trends.charts-render',
    'dashboard',
    async () => {
      /* charts render from the server payload triggered by the toggle */
    },
    async () => {
      await expect(page.locator('.recharts-surface').first()).toBeVisible({
        timeout: 75_000,
      });
    },
    { deadlineMs: 90_000 }
  );

  await timedAction(
    page,
    'dash.trends.period.7d',
    'dashboard',
    async () => {
      await page.getByRole('button', { name: /^7d$/i }).click();
    },
    async () => {
      await page.waitForLoadState('networkidle', { timeout: 75_000 });
    },
    { deadlineMs: 90_000 }
  );

  await timedAction(
    page,
    'dash.toggle.back-to-today',
    'dashboard',
    async () => {
      await page.getByRole('button', { name: /^today$/i }).click();
    },
    async () => {
      await expect(page.getByText(/top matches/i)).toBeVisible({
        timeout: 45_000,
      });
    }
  );

  await timedAction(
    page,
    'dash.top-match.click-through',
    'dashboard',
    async () => {
      await page.locator('a[href^="/jobs/"]').first().click();
    },
    async () => {
      await expect(page).toHaveURL(/\/jobs\//, { timeout: 20_000 });
      await expect(page.getByText(/score breakdown/i)).toBeVisible({
        timeout: 45_000,
      });
    }
  );

  await timedAction(
    page,
    'insights.redirect',
    'dashboard',
    async () => {
      await page.goto('/insights', { waitUntil: 'domcontentloaded' });
    },
    async () => {
      await expect(page).toHaveURL(/dashboard\?view=trends/, {
        timeout: 20_000,
      });
    }
  );
});

test('jobs list journey', async ({ page }) => {
  test.setTimeout(600_000);

  await timedAction(
    page,
    'jobs.list.load',
    'jobs',
    async () => {
      await page.goto('/jobs', { waitUntil: 'domcontentloaded' });
    },
    async () => {
      await expect(
        page.locator('[aria-label^="Match score"]').first()
      ).toBeVisible({ timeout: 60_000 });
    }
  );

  await timedAction(
    page,
    'jobs.tab.target',
    'jobs',
    async () => {
      await page
        .getByRole('tab', { name: /fullstack|levels/i })
        .or(page.getByRole('button', { name: /fullstack|levels/i }))
        .first()
        .click();
    },
    async () => {
      await expect(
        page.locator('[aria-label^="Match score"]').first()
      ).toBeVisible({ timeout: 60_000 });
    }
  );

  await timedAction(
    page,
    'jobs.tab.all',
    'jobs',
    async () => {
      await page
        .getByRole('tab', { name: /all jobs/i })
        .or(page.getByRole('button', { name: /all jobs/i }))
        .first()
        .click();
    },
    async () => {
      await expect(
        page.locator('[aria-label^="Match score"]').first()
      ).toBeVisible({ timeout: 60_000 });
    }
  );

  for (const [id, header] of [
    ['jobs.sort.company', /company/i],
    ['jobs.sort.posted', /posted/i],
    ['jobs.sort.score-restore', /score/i],
  ] as const) {
    await timedAction(
      page,
      id,
      'jobs',
      async () => {
        await page
          .getByRole('columnheader', { name: header })
          .or(page.getByRole('button', { name: header }))
          .first()
          .click();
      },
      async () => {
        await page.waitForLoadState('networkidle', { timeout: 60_000 });
        await expect(page.locator('tbody tr').first()).toBeVisible({
          timeout: 30_000,
        });
      }
    );
  }

  await timedAction(
    page,
    'jobs.filter.min-score',
    'jobs',
    async () => {
      // shared-ui Dropdown: open-state gating races (#522), so a menu left
      // open by a previous action swallows this click as a dismiss and the
      // menu never opens — the item wait then ate the full 45s timeout.
      // Dismiss first, then PROVE the menu opened before reaching for an item.
      await page.keyboard.press('Escape');
      const scoreTrigger = page.getByRole('button', { name: /any score/i });
      await scoreTrigger.click();
      await expect(scoreTrigger).toHaveAttribute('aria-expanded', 'true');
      await page.getByRole('menuitem', { name: /score 70\+/i }).click();
    },
    async () => {
      await page.waitForLoadState('networkidle', { timeout: 60_000 });
    }
  );

  await timedAction(
    page,
    'jobs.filter.status',
    'jobs',
    async () => {
      await page.getByRole('button', { name: /all statuses/i }).click();
      await page.getByRole('menuitem', { name: /^new$/i }).click();
    },
    async () => {
      await page.waitForLoadState('networkidle', { timeout: 60_000 });
    }
  );

  await timedAction(
    page,
    'jobs.filter.remote-only',
    'jobs',
    async () => {
      await page.getByRole('button', { name: /remote only/i }).click();
    },
    async () => {
      await page.waitForLoadState('networkidle', { timeout: 60_000 });
    }
  );

  await timedAction(
    page,
    'jobs.filter.min-salary',
    'jobs',
    async () => {
      await page.getByRole('button', { name: /any pay/i }).click();
      await page.getByRole('menuitem', { name: /\$150k\+/i }).click();
    },
    async () => {
      await page.waitForLoadState('networkidle', { timeout: 60_000 });
    }
  );

  await timedAction(
    page,
    'jobs.filter.clear',
    'jobs',
    async () => {
      await page.getByRole('button', { name: /clear all/i }).click();
    },
    async () => {
      await page.waitForLoadState('networkidle', { timeout: 60_000 });
    }
  );

  await timedAction(
    page,
    'jobs.search.title',
    'jobs',
    async () => {
      const box = page.getByPlaceholder(/search by title/i);
      await box.fill('engineer');
    },
    async () => {
      await page.waitForLoadState('networkidle', { timeout: 60_000 });
      await expect(page.locator('tbody tr').first()).toBeVisible({
        timeout: 30_000,
      });
    }
  );

  await timedAction(
    page,
    'jobs.load-more',
    'jobs',
    async () => {
      await page.getByPlaceholder(/search by title/i).fill('');
      await page.waitForTimeout(600);
      const btn = page.getByRole('button', { name: /load more/i });
      await btn.scrollIntoViewIfNeeded();
      await btn.click();
    },
    async () => {
      await page.waitForLoadState('networkidle', { timeout: 60_000 });
    }
  );

  // Panel flows on the ALL-JOBS tab: no targetId → the panel does NOT
  // auto-fire an LLM analysis, so these rows stay spend-free.
  await timedAction(
    page,
    'jobs.row.expand-panel',
    'jobs',
    async () => {
      await page.locator('tbody tr td:nth-child(5)').first().click();
    },
    async () => {
      await expect(page.getByText(/score breakdown/i)).toBeVisible({
        timeout: 30_000,
      });
    }
  );

  await timedAction(
    page,
    'jobs.panel.breakdown-render',
    'jobs',
    async () => {
      /* rendered by the expand — this row times the axes/keyword list */
    },
    async () => {
      await expect(
        page.getByText(/title fit|role titles/i).first()
      ).toBeVisible({ timeout: 30_000 });
    }
  );

  await timedAction(
    page,
    'jobs.panel.status-history',
    'jobs',
    async () => {
      /* history fetch fires on panel mount; assert it landed */
    },
    async () => {
      await page.waitForLoadState('networkidle', { timeout: 30_000 });
    }
  );

  await timedAction(
    page,
    'jobs.panel.status.to-saved',
    'jobs',
    async () => {
      const panel = page.locator('div.bg-surface-tertiary').last();
      await panel.getByText(/^new$/i).first().click();
      await page.getByRole('menuitem', { name: /^saved$/i }).click();
    },
    async () => {
      await expect(
        page
          .locator('div.bg-surface-tertiary')
          .last()
          .getByText(/^saved$/i)
          .first()
      ).toBeVisible({ timeout: 30_000 });
    }
  );

  await timedAction(
    page,
    'jobs.panel.status.back-to-new',
    'jobs',
    async () => {
      const panel2 = page.locator('div.bg-surface-tertiary').last();
      await panel2
        .getByText(/^saved$/i)
        .first()
        .click();
      await page.getByRole('menuitem', { name: /^new$/i }).click();
    },
    async () => {
      await expect(page.getByText(/^new$/i).first()).toBeVisible({
        timeout: 30_000,
      });
    }
  );

  await timedAction(
    page,
    'jobs.panel.close',
    'jobs',
    async () => {
      await page.locator('tbody tr td:nth-child(5)').first().click();
    },
    async () => {
      await page.waitForTimeout(300);
    }
  );

  // LLM row: the panel auto-fires the analysis on expand (there is no
  // spend-free panel open — probe-proven). Time the verdict completion.
  await timedAction(
    page,
    'jobs.panel.analysis.llm-run',
    'jobs',
    async () => {
      await page.locator('tbody tr td:nth-child(5)').first().click();
      const href = await page
        .locator('div.bg-surface-tertiary a[href^="/jobs/"]')
        .first()
        .getAttribute('href')
        .catch(() => null);
      analysisJobId = href ? href.split('/jobs/')[1] : null;
    },
    async () => {
      await expect(page.getByText('LLM Analysis')).toBeVisible({
        timeout: 30_000,
      });
      await expect(
        page.getByText(/analyzing this job against your profile/i)
      ).toBeHidden({ timeout: 240_000 });
      await expect(
        page.getByRole('button', { name: /not for me/i })
      ).toBeVisible({ timeout: 15_000 });
    },
    { deadlineMs: 300_000 }
  );
});

test('job detail + tailor journey', async ({ page }) => {
  test.setTimeout(900_000);
  test.skip(!analysisJobId, 'no job captured from the analysis row');
  const jobId = analysisJobId as string;

  await timedAction(
    page,
    'jobdetail.page.load',
    'jobdetail',
    async () => {
      await page.goto(`/jobs/${jobId}`, { waitUntil: 'domcontentloaded' });
    },
    async () => {
      await expect(page.getByText(/score breakdown/i)).toBeVisible({
        timeout: 60_000,
      });
    }
  );

  await timedAction(
    page,
    'jobdetail.axes-breakdown.render',
    'jobdetail',
    async () => {
      /* rendered with the page; this row isolates the breakdown paint */
    },
    async () => {
      await expect(
        page.getByText(/title fit|role titles/i).first()
      ).toBeVisible({ timeout: 30_000 });
    }
  );

  await timedAction(
    page,
    'jobdetail.description.toggle',
    'jobdetail',
    async () => {
      // Native <details>/<summary>, EXPANDED by default and carrying no
      // role=button (DOM-probed 2026-08-07). The old
      // getByRole('button', …).click().catch(…) therefore never matched:
      // it burned the full 45s actionTimeout and the swallowed error
      // logged the row as a 45,4xx ms PASS on every single run.
      await page
        .locator('details')
        .filter({ hasText: /job description/i })
        .first()
        .locator('summary')
        .first()
        .click();
    },
    async () => {
      // Assert the toggle actually flipped state — open -> closed. Without
      // this the action can 'succeed' while doing nothing at all.
      // getAttribute, not evaluate(): the e2e tsconfig ships no DOM lib, so
      // DOM types are unavailable inside evaluate callbacks. <details> drops
      // the `open` attribute entirely when collapsed.
      await expect
        .poll(
          async () =>
            page
              .locator('details')
              .filter({ hasText: /job description/i })
              .first()
              .getAttribute('open'),
          { timeout: 5_000 }
        )
        .toBeNull();
    }
  );

  // ---- Resume generation (LLM, budgeted) + full review flows ------------
  await timedAction(
    page,
    'resume.generate.llm',
    'tailor',
    async () => {
      await page
        .getByRole('button', { name: /generate tailored resume/i })
        .click();
    },
    async () => {
      await expect(
        page
          .getByRole('link', { name: /review tailored resume/i })
          .or(page.getByText(/resume ready|review resume/i))
          .first()
      ).toBeVisible({ timeout: 180_000 });
    },
    { deadlineMs: 300_000 }
  );

  await timedAction(
    page,
    'resume.page.load',
    'tailor',
    async () => {
      await page.goto(`/jobs/${jobId}/resume`, {
        waitUntil: 'domcontentloaded',
      });
    },
    async () => {
      await expect(page.getByLabel('Resume markdown')).toBeVisible({
        timeout: 60_000,
      });
    }
  );

  await timedAction(
    page,
    'resume.edit.autosave',
    'tailor',
    async () => {
      const editor = page.getByLabel('Resume markdown');
      await editor.click();
      await page.keyboard.press('End');
      await page.keyboard.type(' ');
      await page.keyboard.press('Backspace');
    },
    async () => {
      await expect(page.getByText(/^saved$|all changes saved/i)).toBeVisible({
        timeout: 30_000,
      });
    }
  );

  await timedAction(
    page,
    'resume.versions.open',
    'tailor',
    async () => {
      await page.getByRole('button', { name: /show/i }).first().click();
    },
    async () => {
      await page.waitForLoadState('networkidle', { timeout: 30_000 });
    }
  );

  await timedAction(
    page,
    'resume.checkpoint',
    'tailor',
    async () => {
      const res = await page.request.post(
        `/api/jobs/tailor/${await recordIdFromPage(page, jobId)}/checkpoint`,
        {}
      );
      if (!res.ok()) throw new Error(`checkpoint ${res.status()}`);
    },
    async () => {
      /* asserted in act */
    }
  );

  await timedAction(
    page,
    'resume.approve',
    'tailor',
    async () => {
      // The trigger is an icon-only button with NO accessible name — no text,
      // no aria-label, only aria-haspopup="menu" (DOM-probed 2026-08-07).
      // getByRole('button', {name: /open more menu/i}) could never match.
      // Selecting structurally until the a11y bug is fixed; then this should
      // move back to an accessible-name query.
      const moreMenu = page.locator('button[aria-haspopup="menu"]').first();
      await page.keyboard.press('Escape');
      await moreMenu.click();
      await expect(moreMenu).toHaveAttribute('aria-expanded', 'true');
      await page.getByText(/lock from editing/i).click();
    },
    async () => {
      await expect(page.getByText(/locked/i).first()).toBeVisible({
        timeout: 30_000,
      });
    }
  );

  await timedAction(
    page,
    'resume.unapprove',
    'tailor',
    async () => {
      // The trigger is an icon-only button with NO accessible name — no text,
      // no aria-label, only aria-haspopup="menu" (DOM-probed 2026-08-07).
      // getByRole('button', {name: /open more menu/i}) could never match.
      // Selecting structurally until the a11y bug is fixed; then this should
      // move back to an accessible-name query.
      const moreMenu = page.locator('button[aria-haspopup="menu"]').first();
      await page.keyboard.press('Escape');
      await moreMenu.click();
      await expect(moreMenu).toHaveAttribute('aria-expanded', 'true');
      await page.getByText(/unlock for editing/i).click();
    },
    async () => {
      await expect(page.getByLabel('Resume markdown')).toBeVisible({
        timeout: 30_000,
      });
    }
  );

  await timedAction(
    page,
    'resume.download.docx',
    'tailor',
    async () => {
      const dl = page.waitForEvent('download', { timeout: 60_000 });
      await page
        .getByRole('button', { name: /download resume as \.docx/i })
        .click();
      const download = await dl;
      if (!download.suggestedFilename().endsWith('.docx')) {
        throw new Error('not a docx');
      }
    },
    async () => {
      /* asserted in act */
    }
  );

  // ---- Cover letter (LLM, budgeted) -------------------------------------
  await timedAction(
    page,
    'cover.generate.llm',
    'tailor',
    async () => {
      await page.goto(`/jobs/${jobId}`, { waitUntil: 'domcontentloaded' });
      await expect(page.getByText(/score breakdown/i)).toBeVisible({
        timeout: 60_000,
      });
      await page
        .getByRole('button', { name: /generate cover letter/i })
        .click();
    },
    async () => {
      await expect(
        page
          .getByRole('link', { name: /review cover letter/i })
          .or(page.getByText(/cover letter ready/i))
          .first()
      ).toBeVisible({ timeout: 180_000 });
    },
    { deadlineMs: 300_000 }
  );

  await timedAction(
    page,
    'cover.page.load',
    'tailor',
    async () => {
      await page.goto(`/jobs/${jobId}/cover-letter`, {
        waitUntil: 'domcontentloaded',
      });
    },
    async () => {
      await expect(
        page.getByLabel(/cover letter markdown|markdown/i).first()
      ).toBeVisible({ timeout: 60_000 });
    }
  );

  await timedAction(
    page,
    'cover.download.docx',
    'tailor',
    async () => {
      const dl = page.waitForEvent('download', { timeout: 60_000 });
      await page
        .getByRole('button', { name: /download.*\.docx/i })
        .first()
        .click();
      await dl;
    },
    async () => {
      /* asserted in act */
    }
  );
});

async function recordIdFromPage(
  page: import('@playwright/test').Page,
  jobId: string
): Promise<string> {
  const res = await page.request.get(`/api/jobs/tailor/by-job/${jobId}`);
  const body = (await res.json()) as { id: string };
  return body.id;
}

test('targets + profile + settings + onboarding', async ({ page }) => {
  test.setTimeout(600_000);

  await timedAction(
    page,
    'targets.list.load',
    'targets',
    async () => {
      await page.goto('/targets', { waitUntil: 'domcontentloaded' });
    },
    async () => {
      await expect(
        page.getByText(/fullstack|software engineer/i).first()
      ).toBeVisible({ timeout: 45_000 });
    }
  );

  await timedAction(
    page,
    'targets.detail.load',
    'targets',
    async () => {
      await page.goto(`/targets/${TARGET_ID}`, {
        waitUntil: 'domcontentloaded',
      });
    },
    async () => {
      await expect(
        page.getByText(/scoring profile|keywords|preferences/i).first()
      ).toBeVisible({ timeout: 45_000 });
    }
  );

  for (const [id, api] of [
    ['targets.detail.status-poll', `/api/targets/${TARGET_ID}/status`],
    [
      'targets.detail.preferences.load',
      `/api/targets/${TARGET_ID}/preferences`,
    ],
    [
      'targets.detail.reference-jds.load',
      `/api/targets/${TARGET_ID}/reference-jds`,
    ],
    [
      'targets.detail.learning-log.load',
      `/api/targets/${TARGET_ID}/learning-log?limit=50`,
    ],
  ] as const) {
    await timedAction(
      page,
      id,
      'targets',
      async () => {
        const res = await page.request.get(api);
        if (!res.ok()) throw new Error(`${api} ${res.status()}`);
      },
      async () => {
        /* asserted in act */
      }
    );
  }

  await timedAction(
    page,
    'profile.page.load',
    'profile',
    async () => {
      await page.goto('/profile', { waitUntil: 'domcontentloaded' });
    },
    async () => {
      await expect(
        page.getByText(/experience|identity|profile/i).first()
      ).toBeVisible({ timeout: 45_000 });
    }
  );

  for (const [id, api] of [
    ['profile.identity.load', '/api/profile/identity'],
    ['profile.experience.prose.load', '/api/career/experience/prose'],
    ['profile.experience.optimized.load', '/api/career/experience/optimized'],
    ['profile.gap-health.load', '/api/career/experience/gap-health'],
    ['profile.resume-style.load', '/api/profile/resume-style'],
    ['profile.llm-usage.load', '/api/profile/llm-usage'],
  ] as const) {
    await timedAction(
      page,
      id,
      'profile',
      async () => {
        const res = await page.request.get(api);
        if (!res.ok()) throw new Error(`${api} ${res.status()}`);
      },
      async () => {
        /* asserted in act */
      }
    );
  }

  await timedAction(
    page,
    'settings.page.load',
    'settings',
    async () => {
      await page.goto('/settings', { waitUntil: 'domcontentloaded' });
    },
    async () => {
      await expect(
        page.getByText(/notifications|billing|plan/i).first()
      ).toBeVisible({ timeout: 45_000 });
    }
  );

  for (const [id, api] of [
    ['settings.notifications.load', '/api/profile/notifications'],
    ['settings.keys.load', '/api/profile/keys'],
    ['settings.billing.account.load', '/api/billing/account'],
  ] as const) {
    await timedAction(
      page,
      id,
      'settings',
      async () => {
        const res = await page.request.get(api);
        if (!res.ok()) throw new Error(`${api} ${res.status()}`);
      },
      async () => {
        /* asserted in act */
      }
    );
  }

  await timedAction(
    page,
    'onboarding.completed-redirect',
    'onboarding',
    async () => {
      await page.goto('/onboarding', { waitUntil: 'domcontentloaded' });
    },
    async () => {
      await expect(page).toHaveURL(/\/dashboard/, { timeout: 20_000 });
    }
  );

  // Restore the activation state EXACTLY as found — do not assume it was off.
  // A sweep that leaves the owner's only active target deactivated leaves
  // their /jobs list empty until someone notices (2026-08-08).
  await timedAction(
    page,
    'targets.deactivate-restore',
    'targets',
    async () => {
      // Null means setup never ran (its own failure is reported separately);
      // deactivating on a guess is precisely the bug this replaced.
      if (wasActiveBeforeSweep === null) return;
      if (wasActiveBeforeSweep) {
        // It was on before us and it is on now — nothing to undo.
        return;
      }
      const res = await page.request.post(
        `/api/targets/${TARGET_ID}/deactivate`
      );
      if (!res.ok()) throw new Error(`deactivate ${res.status()}`);
    },
    async () => {
      if (wasActiveBeforeSweep === null) return;
      const active = await readIsActive(page);
      expect(
        active,
        `activation not restored: it was ${wasActiveBeforeSweep} before the ` +
          `sweep and is ${active} after. Leaving the owner's target off ` +
          `empties their /jobs list.`
      ).toBe(wasActiveBeforeSweep);
    }
  );
});
