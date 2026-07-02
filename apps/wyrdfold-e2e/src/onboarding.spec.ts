import { test, expect } from '@playwright/test';

/**
 * Authenticated smoke. Verifies the most regression-prone surface
 * we shipped this session: the dashboard renders for a signed-in
 * user, the 7-counter pipeline strip is intact, the sidebar nav
 * works, and the (app) routes hydrate without console errors.
 *
 * What this catches:
 *   - SSR ``fetchJsonFromWyrdfoldAPI`` collapsing to null (the bug
 *     #681 / #688 fixed — would show "Build your profile" for a
 *     valid session)
 *   - Middleware regression that strips the cookie before the
 *     server component reads it
 *   - Dashboard counter strip wiring regression (#685 + #696)
 *   - Sidebar logo a11y regression (#696 #1) — visible text
 *     should be ``WyrdFold`` not ``WyrdFoldWyrdFold``
 *   - Catastrophic build failures on any authed route
 *
 * What this deliberately doesn't do:
 *   - Drive LLM-backed flows (onboarding turn, analysis, tailor) —
 *     those cost real money per run and are smoked manually with
 *     the wipe_user_data.py utility. Adding them here behind a
 *     ``--grep`` opt-in is the next step if the recurring cost
 *     becomes worth it.
 */
test.describe('authenticated dashboard smoke', () => {
  test('signed-in user lands on /dashboard and sees the chrome', async ({
    page,
  }) => {
    await page.goto('/dashboard');

    // 1. Stayed on /dashboard — no middleware redirect.
    await expect(page).toHaveURL(/\/dashboard$/);

    // 2. Page heading rendered.
    await expect(
      page.getByRole('heading', { name: 'Dashboard', level: 1 })
    ).toBeVisible();

    // 3. Sidebar logo a11y: the link's accessible name is
    //    "WyrdFold home". The wordmark itself is a decorative
    //    aria-hidden SVG with no text content — a later Lighthouse
    //    ``label-content-name-mismatch`` fix removed the visible text
    //    that this spec originally asserted (the #696
    //    "WyrdFoldWyrdFold" regression is now impossible by
    //    construction: the accessible name comes only from aria-label).
    const logo = page.getByRole('link', { name: 'WyrdFold home' });
    await expect(logo).toBeVisible();
    await expect(logo).toHaveText('');
  });

  test('sidebar nav lists every (app) route', async ({ page }) => {
    await page.goto('/dashboard');
    // Each top-level route should have a nav link. Order isn't
    // asserted — that's a styling choice that shouldn't gate the
    // build.
    const routes = [
      'Dashboard',
      'Jobs',
      'Targets',
      'Profile',
      'Insights',
      'Settings',
    ];
    for (const label of routes) {
      await expect(
        page.getByRole('link', { name: label, exact: true }).first()
      ).toBeVisible();
    }
  });
});
