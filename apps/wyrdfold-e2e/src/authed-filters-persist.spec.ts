import { test, expect } from './fixtures';
import { createClient } from '@supabase/supabase-js';

/**
 * Filter persistence across a bare re-entry to /jobs.
 *
 * The /jobs filters live in the URL (authoritative) with a per-target
 * localStorage snapshot behind it; entering /jobs with no query string
 * (sidebar link, dashboard CTA) restores the snapshot into the URL.
 * Historically the snapshot only carried the original five dimensions —
 * the logistics filters (#86: remote/salary/country) were URL-only and
 * silently reset on every bare re-entry. The shared field list
 * (``jobsFilterFields.ts``) made persistence cover all dimensions; this
 * spec pins the full journey in a real browser, logistics included.
 *
 * Owns its data setup (an active target linked to the e2e user) because
 * the /jobs filter toolbar only renders when the user has an active
 * target — the shared smoke specs deliberately seed nothing.
 *
 * What this catches:
 *   - A filter dimension added to the URL/API layers but not
 *     persistence (the original drift class).
 *   - The restore effect regressing (bare re-entry loses ALL filters).
 *   - localStorage snapshot writes breaking (nothing to restore).
 */

// Fixed sentinel id so reruns upsert the same rows instead of multiplying.
const TARGET_ID = '00000000-0000-4000-8000-0000000f1175';

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
    label: 'e2e-filters-target',
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

test('filters — logistics included — survive a bare re-entry to /jobs', async ({
  page,
}) => {
  // Land with a mixed filter set: one legacy dimension (status) + two
  // logistics dimensions (the previously-unpersisted class).
  await page.goto(
    `/jobs?target=${TARGET_ID}&s=new&remote_only=true&min_salary=150000`
  );

  // The active-filter chips rendering proves the URL state hydrated and
  // the localStorage snapshot write effect has run.
  await expect(
    page.getByLabel('Active filters').getByText('Remote only')
  ).toBeVisible();

  // Leave via a normal in-app navigation...
  await page.goto('/dashboard');
  await expect(page.locator('h1').first()).toBeVisible();

  // ...and come back BARE — the sidebar-link shape that used to lose
  // the logistics filters.
  await page.goto(`/jobs?target=${TARGET_ID}`);

  // The restore effect replays the snapshot into the URL.
  await expect(page).toHaveURL(/remote_only=true/);
  await expect(page).toHaveURL(/min_salary=150000/);
  await expect(page).toHaveURL(/s=new/);
  await expect(
    page.getByLabel('Active filters').getByText('Remote only')
  ).toBeVisible();
});
