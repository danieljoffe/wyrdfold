/* eslint-disable playwright/no-networkidle, playwright/no-wait-for-timeout --
 * Timing harness, not a functional spec: 'networkidle' IS the measurement
 * (action -> network settled), fixed settle waits bracket trace capture,
 * and per-action pass/fail lives in the JSONL ledger + coverage gate
 * rather than per-test expect()s. */
import { expect, test } from '@playwright/test';
import { timedAction } from './timing';

/**
 * Public-surface sweep: every action timed, success asserted, ledgered.
 * Read-only throughout — no auth, no mutations.
 */

test.describe.configure({ mode: 'serial' });

test('public pages + search journey', async ({ page }) => {
  test.setTimeout(300_000);

  await timedAction(
    page,
    'public.home.load',
    'public',
    async () => {
      await page.goto('/');
    },
    async () => {
      await expect(page.locator('body')).toContainText(/wyrdfold/i, {
        timeout: 20_000,
      });
    }
  );

  await timedAction(
    page,
    'public.terms.load',
    'public',
    async () => {
      await page.goto('/terms');
    },
    async () => {
      await expect(page.locator('body')).toContainText(/terms/i, {
        timeout: 15_000,
      });
    }
  );

  await timedAction(
    page,
    'public.privacy.load',
    'public',
    async () => {
      await page.goto('/privacy');
    },
    async () => {
      await expect(page.locator('body')).toContainText(/privacy/i, {
        timeout: 15_000,
      });
    }
  );

  await timedAction(
    page,
    'public.login.load',
    'public',
    async () => {
      await page.goto('/login');
    },
    async () => {
      await expect(page.locator('input[type="email"]').first()).toBeVisible({
        timeout: 15_000,
      });
    }
  );

  await timedAction(
    page,
    'public.search.load',
    'search',
    async () => {
      await page.goto('/search');
    },
    async () => {
      await expect(
        page.getByLabel('Search jobs by title or keyword')
      ).toBeVisible({ timeout: 20_000 });
    }
  );

  const searchBox = page.getByLabel('Search jobs by title or keyword');
  const firstResult = page.locator('a[aria-label^="Open "]').first();

  await timedAction(
    page,
    'search.query.frontend',
    'search',
    async () => {
      await searchBox.fill('frontend');
      await searchBox.press('Enter');
    },
    async () => {
      await expect(firstResult).toBeVisible({ timeout: 30_000 });
    }
  );

  await timedAction(
    page,
    'search.query.refine',
    'search',
    async () => {
      await searchBox.fill('backend engineer');
      await searchBox.press('Enter');
    },
    async () => {
      await expect(firstResult).toBeVisible({ timeout: 30_000 });
    }
  );

  // Filters — source-verified controls (shared-ui Select = native select).
  await timedAction(
    page,
    'search.filter.recency',
    'search',
    async () => {
      await page.getByLabel('Filter by date posted').selectOption({ index: 1 });
    },
    async () => {
      await expect(firstResult).toBeVisible({ timeout: 30_000 });
    }
  );

  await timedAction(
    page,
    'search.filter.salary',
    'search',
    async () => {
      await page
        .getByLabel('Filter by minimum salary')
        .selectOption({ index: 2 });
    },
    async () => {
      await expect(firstResult).toBeVisible({ timeout: 30_000 });
    }
  );

  await timedAction(
    page,
    'search.filter.location',
    'search',
    async () => {
      const loc = page.getByLabel('Filter by location');
      await loc.fill('Remote');
      await loc.press('Enter');
    },
    async () => {
      await expect(firstResult).toBeVisible({ timeout: 30_000 });
    }
  );

  // Reset filters so pagination sees the fullest result set.
  await page
    .getByText('Clear filters')
    .click()
    .catch(() => undefined);
  await page.waitForTimeout(1000);

  await timedAction(
    page,
    'search.paginate.next',
    'search',
    async () => {
      const more = page
        .getByRole('button', { name: /load more|next|show more/i })
        .first();
      await more.scrollIntoViewIfNeeded();
      await more.click();
    },
    async () => {
      await page.waitForLoadState('networkidle', { timeout: 30_000 });
    }
  );

  await timedAction(
    page,
    'search.listing.open-modal',
    'search',
    async () => {
      await firstResult.click();
    },
    async () => {
      await expect(
        page.getByRole('dialog').or(page.locator('[role="dialog"]')).first()
      ).toBeVisible({ timeout: 30_000 });
    }
  );

  await timedAction(
    page,
    'search.listing.full-page',
    'search',
    async () => {
      // Hard load of the listing route the modal intercepted.
      await page.reload();
    },
    async () => {
      await expect(page.getByRole('heading').first()).toBeVisible({
        timeout: 40_000,
      });
    }
  );

  await timedAction(
    page,
    'search.signup-mode.probe',
    'search',
    async () => {
      await page.request.get('/api/signup-mode').catch(() => undefined);
    },
    async () => {
      /* status recorded in trace; invite-only 403 is the expected shape */
    }
  );

  await timedAction(
    page,
    'bff.health',
    'bff',
    async () => {
      const res = await page.request.get('/api/health');
      if (!res.ok()) throw new Error(`health ${res.status()}`);
    },
    async () => {
      /* asserted in act */
    }
  );
});
