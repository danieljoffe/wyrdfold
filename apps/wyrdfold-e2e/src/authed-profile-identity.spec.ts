import { test, expect } from './fixtures';
import { createClient } from '@supabase/supabase-js';

/**
 * Authenticated round-trip for the Profile identity card (added in
 * #703 / extended in F3-A; moved from /settings to /profile in the
 * settings refactor — see ``SettingsPage.tsx`` "Identity ...
 * lives on /profile now"). Verifies the full FE → API → DB → API → FE
 * loop without burning LLM credits:
 *
 *   1. ``GET /api/profile/identity`` populates the Name input on
 *      mount.
 *   2. Editing Name triggers the debounced autosave (800ms) which
 *      ``PATCH``-es ``/api/profile/identity``.
 *   3. Reloading the page re-runs the GET and the new value is
 *      visible.
 *
 * Restores the original Name in a ``finally`` block so the shared
 * test user identity stays clean. Other identity fields aren't
 * touched — the autosave PATCHes all six on each diff, but the FE
 * sends the same values it just read from the server for the
 * untouched fields, so they round-trip without change.
 *
 * What this catches:
 *   - GET /identity wiring regression (broken auth / RLS /
 *     ``_get_or_create_profile``).
 *   - PATCH /identity regression (Pydantic shape drift, empty-string
 *     clear-to-NULL behavior, autosave debouncer never firing).
 *   - The Profile page never re-syncing from the server response
 *     (the page does a setState from the PATCH response in the
 *     handleSaveProfile path — this catches a regression where it
 *     drops that re-sync).
 */
// Own the data setup like the other authed specs: a fresh stack's e2e
// user has no profile row / NULL name, and the round-trip below needs a
// non-empty starting value to restore to. Only writes when missing so a
// long-lived environment's real name is left alone.
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

  const { data: rows, error: selErr } = await admin
    .from('user_profiles')
    .select('id, name')
    .eq('user_id', user.id)
    .limit(1);
  if (selErr) throw new Error(`user_profiles select failed: ${selErr.message}`);
  if (rows?.length) {
    if (!rows[0].name) {
      const { error: updErr } = await admin
        .from('user_profiles')
        .update({ name: 'E2E Seed User' })
        .eq('user_id', user.id);
      if (updErr)
        throw new Error(`user_profiles update failed: ${updErr.message}`);
    }
  } else {
    const { error: insErr } = await admin
      .from('user_profiles')
      .insert({ user_id: user.id, name: 'E2E Seed User', email });
    if (insErr)
      throw new Error(`user_profiles insert failed: ${insErr.message}`);
  }
});

test.describe('profile identity round-trip', () => {
  test('Name edits persist across reload', async ({ page }) => {
    const TEST_NAME = `E2E Test User ${Date.now()}`;

    await page.goto('/profile');

    // Wait for the Profile page to mount. The Name input lives in
    // ProfileIdentityCard; the Skeleton is replaced once GET /identity
    // resolves.
    const nameInput = page.getByRole('textbox', { name: /^Name\b/ });
    await expect(nameInput).toBeVisible();

    // The beforeAll guarantees the user has a non-empty name, but the
    // card renders its inputs immediately and only fills them once
    // GET /identity resolves — reading the value at first visibility
    // races that fetch (flaked on slower stacks). Wait for the seeded
    // value to land instead.
    await expect(nameInput).not.toHaveValue('', { timeout: 5_000 });
    const original = await nameInput.inputValue();
    expect(original.length).toBeGreaterThan(0);

    try {
      await nameInput.fill(TEST_NAME);

      // Autosave is debounced ~800ms; the SavingIndicator pops on
      // and off around the network call. Wait for the cycle to
      // complete — once the indicator is gone, the PATCH has
      // returned.
      const savingIndicator = page.getByText('Saving…');
      await expect(savingIndicator).toBeVisible({ timeout: 2_000 });
      await expect(savingIndicator).toBeHidden({ timeout: 5_000 });

      // Hard reload — re-runs SSR + client mount, so GET /identity
      // fires fresh against the database.
      await page.reload();

      const reloadedNameInput = page.getByRole('textbox', { name: /^Name\b/ });
      await expect(reloadedNameInput).toHaveValue(TEST_NAME);
    } finally {
      // Restore. If the previous step crashed mid-test we still
      // want to leave the shared user's identity in its original
      // state; otherwise subsequent runs would assert against
      // ``E2E Test User <timestamp>`` and the manual UI would show
      // a confusing stale name.
      const restoreInput = page.getByRole('textbox', { name: /^Name\b/ });
      if ((await restoreInput.inputValue()) !== original) {
        await restoreInput.fill(original);
        // The autosave is debounced (~800ms), so the indicator must
        // APPEAR before "hidden" means the PATCH completed — waiting
        // only for hidden resolves instantly and closes the page before
        // the restore ever fires (exactly how a stale test name got
        // left in the DB). Best-effort both ways — don't fail the test
        // on cleanup races.
        await page
          .getByText('Saving…')
          .waitFor({ state: 'visible', timeout: 3_000 })
          .catch(() => {});
        await page
          .getByText('Saving…')
          .waitFor({ state: 'hidden', timeout: 5_000 })
          .catch(() => {});
      }
    }
  });
});
