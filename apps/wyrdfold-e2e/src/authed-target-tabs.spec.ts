import { test, expect } from '@playwright/test';
import { createClient } from '@supabase/supabase-js';

/**
 * Target-detail tabs (UX/IA Forks B+C, PR #335) — the real-browser journey.
 *
 * The per-target page splits into four URL-backed tabs (Scoring ·
 * Preferences · Reference JDs · Learning) with the Reference JDs list
 * fetched lazily on first activation, and the Scoring tab badging what is
 * shared with co-searchers vs. per-user. The component specs pin the
 * composition; this spec pins the assembled journey — real clicks driving
 * the BFF → API round-trips and the ?tab= URL contract.
 *
 * Owns its data setup (an active target linked to the e2e user) like
 * authed-filters-persist — a distinct sentinel so the two specs never
 * write over each other's rows in a parallel run.
 *
 * What this catches:
 *   - Tab activation breaking the ?tab= URL contract (deep links, back
 *     button, and the scoring-is-default param cleanup).
 *   - The lazy JD fetch firing on page load (perf regression) or never
 *     firing (dead tab).
 *   - The shared-vs-yours badging disappearing from the scoring tab.
 */

// Fixed sentinel id so reruns upsert the same rows instead of multiplying.
const TARGET_ID = '00000000-0000-4000-8000-0000000f1176';

test.beforeAll(async () => {
  const url = process.env['NEXT_PUBLIC_SUPABASE_URL'];
  const serviceKey = process.env['SUPABASE_SERVICE_ROLE_KEY'];
  const email = process.env['E2E_TEST_USER_EMAIL'];
  test.skip(!url || !serviceKey || !email, 'auth env not configured');

  const admin = createClient(url!, serviceKey!, {
    auth: { persistSession: false, autoRefreshToken: false },
  });

  const { data: userList, error: listErr } = await admin.auth.admin.listUsers();
  if (listErr) throw new Error(`listUsers failed: ${listErr.message}`);
  const user = userList.users.find(u => u.email === email);
  if (!user) throw new Error(`e2e user ${email} not found — run seed-e2e-user`);

  const { error: targetErr } = await admin.from('targets').upsert({
    id: TARGET_ID,
    label: 'e2e-tabs-target',
    // P0 re-semantics: no flag on the shared row — the ACTIVE MEMBERSHIP
    // below is what makes this target pipeline-active (app_active is the
    // instance-sponsorship floor, not a user-target concept).
    activation_status: 'ready',
    scoring_profile: {},
  });
  if (targetErr) throw new Error(`target upsert failed: ${targetErr.message}`);

  // user_targets has no upsert conflict target we can rely on across
  // environments — select-then-write keeps the link idempotent.
  const { data: existing, error: selErr } = await admin
    .from('user_targets')
    .select('id')
    .eq('user_id', user.id)
    .eq('target_id', TARGET_ID)
    .limit(1);
  if (selErr) throw new Error(`user_targets select failed: ${selErr.message}`);
  if (existing?.length) {
    const { error: updErr } = await admin
      .from('user_targets')
      .update({ is_active: true })
      .eq('user_id', user.id)
      .eq('target_id', TARGET_ID);
    if (updErr)
      throw new Error(`user_targets update failed: ${updErr.message}`);
  } else {
    const { error: linkErr } = await admin.from('user_targets').insert({
      user_id: user.id,
      target_id: TARGET_ID,
      is_active: true,
    });
    if (linkErr)
      throw new Error(`user_targets insert failed: ${linkErr.message}`);
  }
});

test('tabs drive the ?tab= URL, lazy-load JDs, and badge shared vs yours', async ({
  page,
}) => {
  const jdRequests: string[] = [];
  page.on('request', req => {
    if (req.url().includes(`/api/targets/${TARGET_ID}/reference-jds`)) {
      jdRequests.push(req.url());
    }
  });

  await page.goto(`/targets/${TARGET_ID}`);

  // Scoring is the default tab: shared-vs-yours badging visible, no ?tab=.
  await expect(page.getByText('Shared with co-searchers')).toBeVisible();
  await expect(page.getByText('Only you')).toBeVisible();
  await expect(page).not.toHaveURL(/tab=/);

  // All four tabs render.
  for (const label of ['Scoring', 'Preferences', 'Reference JDs', 'Learning']) {
    await expect(page.getByRole('tab', { name: label })).toBeVisible();
  }

  // Lazy contract: landing on scoring must NOT have fetched the JD list.
  expect(jdRequests).toHaveLength(0);

  // Preferences → URL carries the tab.
  await page.getByRole('tab', { name: 'Preferences' }).click();
  await expect(page).toHaveURL(/tab=preferences/);

  // Reference JDs → URL updates and the lazy fetch fires exactly now.
  await page.getByRole('tab', { name: 'Reference JDs' }).click();
  await expect(page).toHaveURL(/tab=jds/);
  await expect.poll(() => jdRequests.length).toBeGreaterThan(0);

  // Back to Scoring → default tab cleans the param off the URL.
  await page.getByRole('tab', { name: 'Scoring' }).click();
  await expect(page).not.toHaveURL(/tab=/);
  await expect(page.getByText('Shared with co-searchers')).toBeVisible();

  // Deep link: a non-default tab straight from the URL.
  await page.goto(`/targets/${TARGET_ID}?tab=learning`);
  await expect(page.getByRole('tab', { name: 'Learning' })).toHaveAttribute(
    'aria-selected',
    'true'
  );
});
