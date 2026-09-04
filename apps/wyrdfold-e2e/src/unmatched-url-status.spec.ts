import { test, expect } from './fixtures';

/**
 * #985 regression guard, signed-out half (see the authed twin,
 * ``authed-unmatched-url-status.spec.ts``, for the full story).
 *
 * Contract pinned for visitors with no session:
 * - an unmatched GATED path never reaches routing — the proxy walls it
 *   behind /login first, so route existence isn't probeable logged-out;
 * - an unmatched sub-path under a public allowlist PREFIX
 *   (``/login/<junk>`` passes the proxy's ``startsWith('/login')``
 *   allowlist and matches no route) answers an honest 404 — the one
 *   cookieless window into Next's unmatched-URL status behavior.
 */

test('signed-out: a bogus gated path redirects to /login, not a 404', async ({
  page,
}) => {
  await page.goto('/this-route-does-not-exist-985');
  await expect(page).toHaveURL(/\/login/);
});

test('signed-out: unmatched sub-path under the /login allowlist answers 404', async ({
  page,
}) => {
  const response = await page.goto('/login/junk-985');
  expect(response, 'document navigation yields a response').toBeTruthy();
  expect(response!.status()).toBe(404);
});
