/* eslint-disable playwright/no-networkidle --
 * Adversarial sweep, not a functional spec: 'networkidle' is how we know the
 * page settled, and each surface reports its own verdict into the ledger. */
import { expect, test } from '@playwright/test';
import { timedAction } from './timing';

/**
 * FRONTEND RED TEAM (2026-08-08 overnight).
 *
 * The functional sweeps drive the app the way a user would. This one drives it
 * the way an attacker or a broken client would, and asks a narrower question of
 * every surface: **can I make it crash, hang, or render nothing?**
 *
 * What counts as a failure here:
 *  - an uncaught exception reaching `pageerror` (React error boundary blown, or
 *    no boundary at all — the user sees a blank screen);
 *  - a 5xx on the document request;
 *  - a surface that renders NO discernible content (blank body) rather than an
 *    empty state or an error state;
 *  - a dynamic route given a ghost UUID that 500s instead of 404ing (the
 *    `.single()` → PGRST116 class that bit this repo across 9 sites).
 *
 * What does NOT count:
 *  - console errors from third parties or from deliberate 4xx probes — noisy
 *    and not actionable, so they are recorded but not asserted on;
 *  - a 404 page. A correct 404 is a pass.
 *
 * Nothing here mutates: every request is a GET navigation.
 */

const GHOST = '00000000-0000-4000-8000-000000000000';
const MALFORMED = 'not-a-uuid';

/** Pages that must render for an authed user. */
const AUTHED_PAGES = [
  '/dashboard',
  '/jobs',
  '/targets',
  '/insights',
  '/profile',
  '/settings',
];

/** Public pages — must render with no session at all. */
const PUBLIC_PAGES = ['/', '/privacy', '/terms', '/search'];

/**
 * Hostile query strings against the most parameterised surface. Each must be
 * absorbed — clamped, ignored, or surfaced as an error state — never crash.
 */
const HOSTILE_JOBS_QUERIES = [
  'min_score=abc',
  'min_score=-5',
  'min_score=99999',
  'page_size=0',
  'page_size=100000',
  'sort=../../etc/passwd',
  'sort=score&order=sideways',
  'status=not_a_status',
  'cursor=garbage',
  `cursor=${encodeURIComponent(btoa('{"o":999999999}'))}`,
  'country=<script>alert(1)</script>',
  `company=${encodeURIComponent("' OR '1'='1")}`,
  `search=${encodeURIComponent('A'.repeat(3000))}`,
  `exclude_locations=${encodeURIComponent(','.repeat(500))}`,
  'min_salary=-1',
  'remote_only=maybe',
  `target_id=${MALFORMED}`,
  `target_id=${GHOST}`,
];

/** Dynamic routes fed a ghost and a malformed id. */
const DYNAMIC_ROUTES = [
  (id: string) => `/jobs/${id}`,
  (id: string) => `/jobs/${id}/resume`,
  (id: string) => `/jobs/${id}/cover-letter`,
  (id: string) => `/targets/${id}`,
];

interface Watch {
  pageErrors: string[];
  consoleErrors: string[];
  docStatus: number | null;
}

function watch(page: import('@playwright/test').Page): Watch {
  const w: Watch = { pageErrors: [], consoleErrors: [], docStatus: null };
  page.on('pageerror', e => w.pageErrors.push(String(e)));
  page.on('console', m => {
    if (m.type() === 'error') w.consoleErrors.push(m.text().slice(0, 300));
  });
  page.on('response', r => {
    if (r.request().resourceType() === 'document' && w.docStatus === null) {
      w.docStatus = r.status();
    }
  });
  return w;
}

/** A page is "alive" if it rendered real text, not a blank document. */
async function assertAlive(
  page: import('@playwright/test').Page,
  w: Watch,
  where: string
) {
  const bodyText = ((await page.locator('body').innerText()) || '').trim();
  expect(
    bodyText.length,
    `${where} rendered a blank body — no content, no empty state, no error ` +
      `state. docStatus=${w.docStatus} pageErrors=${JSON.stringify(w.pageErrors.slice(0, 2))}`
  ).toBeGreaterThan(0);
  expect(
    w.pageErrors,
    `${where} raised an uncaught exception — a user sees a blank screen or a ` +
      `dead region. These are the ones worth fixing.`
  ).toEqual([]);
  expect(
    w.docStatus === null || w.docStatus < 500,
    `${where} document responded ${w.docStatus}`
  ).toBe(true);
}

test('redteam: authed pages survive hostile query strings', async ({
  page,
}) => {
  test.setTimeout(900_000);

  for (const path of AUTHED_PAGES) {
    const w = watch(page);
    await timedAction(
      page,
      `redteam.page${path.replace(/\//g, '.')}`,
      'redteam',
      async () => {
        await page.goto(path, { waitUntil: 'domcontentloaded' });
        await page
          .waitForLoadState('networkidle', { timeout: 45_000 })
          .catch(() => {});
      },
      async () => {
        await assertAlive(page, w, `GET ${path}`);
      }
    );
  }

  for (const q of HOSTILE_JOBS_QUERIES) {
    const w = watch(page);
    await timedAction(
      page,
      `redteam.jobs.query.${q.split('=')[0]}`,
      'redteam',
      async () => {
        await page.goto(`/jobs?${q}`, { waitUntil: 'domcontentloaded' });
        await page
          .waitForLoadState('networkidle', { timeout: 45_000 })
          .catch(() => {});
      },
      async () => {
        await assertAlive(page, w, `GET /jobs?${q}`);
      }
    );
  }
});

test('redteam: ghost and malformed ids 404 instead of crashing', async ({
  page,
}) => {
  test.setTimeout(900_000);

  for (const mk of DYNAMIC_ROUTES) {
    for (const [kind, id] of [
      ['ghost', GHOST],
      ['malformed', MALFORMED],
    ] as const) {
      const path = mk(id);
      const w = watch(page);
      await timedAction(
        page,
        `redteam.dynamic.${kind}${path.replace(/\//g, '.').replace(id, '')}`,
        'redteam',
        async () => {
          await page.goto(path, { waitUntil: 'domcontentloaded' });
          await page
            .waitForLoadState('networkidle', { timeout: 45_000 })
            .catch(() => {});
        },
        async () => {
          // A 404 is the CORRECT outcome. What must not happen is a 5xx or an
          // uncaught exception — the `.single()` → PGRST116 shape that made
          // nine sites answer 500 to a ghost id.
          await assertAlive(page, w, `GET ${path} (${kind} id)`);
        }
      );
    }
  }
});

test('redteam: public surfaces render with no session', async ({ browser }) => {
  test.setTimeout(900_000);
  // Explicitly session-less: the whole point is the unauthenticated path.
  const ctx = await browser.newContext({
    storageState: { cookies: [], origins: [] },
  });
  const page = await ctx.newPage();

  for (const path of PUBLIC_PAGES) {
    const w = watch(page);
    await timedAction(
      page,
      `redteam.public${path === '/' ? '.home' : path.replace(/\//g, '.')}`,
      'redteam',
      async () => {
        await page.goto(path, { waitUntil: 'domcontentloaded' });
        await page
          .waitForLoadState('networkidle', { timeout: 45_000 })
          .catch(() => {});
      },
      async () => {
        await assertAlive(page, w, `GET ${path} (anon)`);
      }
    );
  }

  // An authed route with no session must redirect or refuse — never render
  // another user's shell, and never crash.
  for (const path of ['/dashboard', '/jobs', '/settings']) {
    const w = watch(page);
    await timedAction(
      page,
      `redteam.anon.gated${path.replace(/\//g, '.')}`,
      'redteam',
      async () => {
        await page.goto(path, { waitUntil: 'domcontentloaded' });
        await page
          .waitForLoadState('networkidle', { timeout: 45_000 })
          .catch(() => {});
      },
      async () => {
        await assertAlive(page, w, `GET ${path} (anon, gated)`);
        const url = page.url();
        const bodyText = (await page.locator('body').innerText()) || '';
        // Either bounced to an auth surface, or shown a gate — but the
        // signed-in shell must not be reachable without a session.
        const bounced =
          !url.includes(path) ||
          /sign in|log in|invite|waitlist|continue with email/i.test(bodyText);
        expect(
          bounced,
          `${path} rendered without a session and without an auth gate — ` +
            `URL stayed ${url} and the body showed no sign-in affordance`
        ).toBe(true);
      }
    );
  }

  await ctx.close();
});
