import { test, expect } from './fixtures';

// #831 / #971: a LOGGED-OUT visitor following a dead shared listing link
// must land on the public not-found — not the member sidebar shell whose
// every link bounces to /login. The unit spec covers the card's content;
// only e2e can prove the full routing composition: /search/[id] page →
// fetchListing 404 → notFound() → search/not-found.tsx → the
// auth-adaptive search layout picking the PUBLIC branch for a guest.
test('dead /search/<id> shows the public not-found to a guest', async ({
  page,
}) => {
  await page.goto('/search/00000000-0000-0000-0000-00000000dead');

  await expect(
    page.getByRole('heading', { name: 'Listing not found', level: 1 })
  ).toBeVisible();
  // The recovery action back into the live pool.
  await expect(page.getByRole('link', { name: 'Browse jobs' })).toHaveAttribute(
    'href',
    '/search'
  );
  // The PUBLIC header, not the member shell: a sign-in link is present and
  // the sidebar's canonical destination is absent.
  await expect(page.getByRole('link', { name: 'Sign in' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Dashboard' })).toHaveCount(0);
});
