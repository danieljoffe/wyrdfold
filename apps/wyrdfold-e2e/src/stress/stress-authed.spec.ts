/* eslint-disable playwright/no-networkidle, playwright/no-wait-for-timeout, playwright/no-skipped-test, playwright/expect-expect --
 * Timing harness, not a functional spec: 'networkidle' IS the measurement
 * (action -> network settled), fixed settle waits bracket trace capture,
 * and per-action pass/fail lives in the JSONL ledger + coverage gate
 * rather than per-test expect()s. */
import { expect, test } from '@playwright/test';
import { timedAction } from './timing';

/**
 * Authed full-app sweep. Serial within one worker; state-restore
 * discipline: the target activated for /jobs coverage is deactivated at
 * the end (the owner had none active), reversible status flips are
 * reverted, approve is followed by unapprove. LLM spend is bounded to one
 * analysis + one resume + one cover letter (~$0.15), all on ONE job so
 * the artifacts land where the owner can find (or delete) them.
 */

const TARGET_ID = '012202b0-e6bd-4617-b9cb-226648e8218e';

test.describe.configure({ mode: 'serial' });

let analysisJobId: string | null = null;

test('setup: activate target for jobs coverage', async ({ page }) => {
  test.setTimeout(120_000);
  await timedAction(
    page,
    'targets.activate',
    'targets',
    async () => {
      const res = await page.request.post(`/api/targets/${TARGET_ID}/activate`);
      if (!res.ok()) throw new Error(`activate ${res.status()}`);
    },
    async () => {
      const res = await page.request.get(
        `/api/targets/${TARGET_ID}/user-target`
      );
      if (!res.ok()) throw new Error(`user-target ${res.status()}`);
    }
  );
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
      await page.getByRole('button', { name: /any score/i }).click();
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
      await page
        .getByRole('button', { name: /description|show/i })
        .first()
        .click()
        .catch(() => undefined);
    },
    async () => {
      await page.waitForTimeout(400);
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
      await page.getByRole('button', { name: /open more menu/i }).click();
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
      await page.getByRole('button', { name: /open more menu/i }).click();
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

  // Restore: the owner had NO active targets before the sweep.
  await timedAction(
    page,
    'targets.deactivate-restore',
    'targets',
    async () => {
      const res = await page.request.post(
        `/api/targets/${TARGET_ID}/deactivate`
      );
      if (!res.ok()) throw new Error(`deactivate ${res.status()}`);
    },
    async () => {
      /* asserted in act */
    }
  );
});
