import { test, expect } from './fixtures';

/**
 * #985 regression guard, signed-in half: an unmatched URL must answer
 * HTTP 404 with the not-found UI — never a soft-404.
 *
 * The issue observed prod serving 200 with the not-found shell on bogus
 * URLs (2026-09-03 smoke). By the time a fix was attempted the premise no
 * longer reproduced — the then-current artifact answered 404 — and the
 * investigation showed WHY the behavior can flap: Next.js documents that
 * a not-found rendered inside a STREAMED response ships status 200, and
 * the root layout's `headers()` read (CSP nonce) makes every route
 * dynamic, so whether the 404 survives depends on build-level streaming
 * behavior that has already differed between deploys of this same app.
 * This pins the honest status so a Next upgrade or layout change that
 * regresses it fails CI (which runs the BUILT production server) instead
 * of surfacing in a release smoke.
 */

test('signed-in: a bogus path answers 404 with the not-found UI', async ({
  page,
}) => {
  const response = await page.goto('/this-route-does-not-exist-985');
  expect(response, 'document navigation yields a response').toBeTruthy();
  expect(response!.status()).toBe(404);
  await expect(page).not.toHaveURL(/\/login/);
  await expect(page.getByText('Page not found')).toBeVisible();
});
