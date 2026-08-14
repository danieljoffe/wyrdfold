/// <reference lib="dom" />
// ^ page.evaluate closures below run in the browser; the e2e tsconfig
//   deliberately omits the DOM lib, so pull it in for this file's sake.
import { createClient } from '@supabase/supabase-js';
import { test, expect } from './fixtures';

/**
 * Search detail modal × Add-to-target picker — the shared-ui 0.12 portal
 * integration drive.
 *
 * 0.12 moved Dropdown menus into a document.body portal so overflow
 * ancestors (this modal's edge — ux-sweep 2026-08-12 §B2) can't clip
 * them. That relocation carries two integration risks this spec pins:
 *
 *  1. THE MENU MUST STILL PAINT, on top of the modal, while no longer
 *     being a DOM descendant of it (the portal precondition — if a
 *     future release de-portals, the containment assertion flips and
 *     this spec fails loudly rather than silently re-clipping).
 *  2. DISMISSAL NESTING: shared-ui Modal listens for Escape on window
 *     and doesn't check ``defaultPrevented``; the keydown from inside
 *     the portaled panel bubbles panel → body → document → window
 *     WITHOUT crossing the app's DOM. The wyrdfold-side guard
 *     (AddToTargetMenu, revised for 0.12) must swallow the Escape the
 *     menu consumed — one keystroke closes the MENU ONLY, the next
 *     closes the modal. Outside-click likewise: closes the menu, never
 *     the modal.
 */

// Fixed sentinel ids so reruns upsert the same rows instead of multiplying.
const TARGET_ID = '00000000-0000-4000-8000-00000f0d1201';
const SOURCE_ID = '00000000-0000-4000-8000-00000f0d1202';
const JOB_ID = '00000000-0000-4000-8000-00000f0d1203';
const JOB_TITLE = 'Portalprobe Staff Engineer';
const TARGET_LABEL = 'e2e-portal-picker-target';

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

  // A target the picker can list.
  const { error: targetErr } = await admin.from('targets').upsert({
    id: TARGET_ID,
    label: TARGET_LABEL,
    activation_status: 'ready',
    scoring_profile: {},
  });
  if (targetErr) throw new Error(`target upsert failed: ${targetErr.message}`);

  // Idempotent membership link (same select-then-write as authed-target-tabs).
  const { data: existing, error: selErr } = await admin
    .from('user_targets')
    .select('id')
    .eq('user_id', user.id)
    .eq('target_id', TARGET_ID)
    .limit(1);
  if (selErr) throw new Error(`user_targets select failed: ${selErr.message}`);
  if (!existing?.length) {
    const { error: linkErr } = await admin.from('user_targets').insert({
      user_id: user.id,
      target_id: TARGET_ID,
      is_active: true,
    });
    if (linkErr)
      throw new Error(`user_targets insert failed: ${linkErr.message}`);
  }

  // A searchable corpus row: must clear the live+US gate
  // (archived/purged NULL, is_us not false) and title-ILIKE-match the
  // query below. Distinctive token keeps other seeds out of the results.
  const { error: sourceErr } = await admin.from('sources').upsert({
    id: SOURCE_ID,
    board_token: 'e2e-portal-probe',
    company_name: 'Portalprobe Inc',
  });
  if (sourceErr) throw new Error(`source upsert failed: ${sourceErr.message}`);

  const { error: jobErr } = await admin.from('jobs').upsert({
    id: JOB_ID,
    source_id: SOURCE_ID,
    external_id: 'e2e-portal-probe-1',
    title: JOB_TITLE,
    company_name: 'Portalprobe Inc',
    description_html: '<p>Drive portaled dropdowns end to end.</p>',
    absolute_url: 'https://example.com/e2e-portal-probe-1',
    location: 'Remote, US',
    is_us: true,
    cataloged_at: new Date().toISOString(),
  });
  if (jobErr) throw new Error(`jobs upsert failed: ${jobErr.message}`);
});

test('portaled picker paints above the modal; Escape and outside-click dismiss the menu, not the modal', async ({
  page,
}) => {
  await page.goto('/search?q=portalprobe');

  // Open the seeded listing's detail modal.
  await page
    .getByRole('link', { name: new RegExp(JOB_TITLE, 'i') })
    .or(page.getByText(JOB_TITLE).first())
    .first()
    .click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toBeVisible();

  // Open the picker and find the portaled menu.
  await dialog
    .getByRole('button', { name: /add to target/i })
    .first()
    .click();
  const menu = page.getByRole('menu');
  await expect(menu).toBeVisible();
  await expect(menu.getByText(TARGET_LABEL)).toBeVisible();

  // Portal precondition + §B2 payoff: the menu is NOT a DOM descendant
  // of the dialog, yet paints on top of it — elementFromPoint at the
  // menu's centre resolves into the menu, so nothing overlays or clips it.
  const { insideDialog, topIsMenu } = await page.evaluate(() => {
    const dialogEl = document.querySelector('[role="dialog"]');
    const menuEl = document.querySelector('[role="menu"]');
    if (!dialogEl || !menuEl) return { insideDialog: null, topIsMenu: null };
    const r = menuEl.getBoundingClientRect();
    const top = document.elementFromPoint(
      r.left + r.width / 2,
      r.top + Math.min(r.height / 2, 20)
    );
    return {
      insideDialog: dialogEl.contains(menuEl),
      topIsMenu: menuEl.contains(top),
    };
  });
  expect(insideDialog).toBe(false); // portaled — de-portaling fails loudly
  expect(topIsMenu).toBe(true); // fully painted, nothing clips it

  // Move focus INTO the portaled panel (the path the old wrapper guard
  // missed), then Escape: menu closes, modal must survive. This is also
  // the discrete-event race: the panel listener's close flushes effect
  // cleanup mid-dispatch, so an open-gated guard would already be gone.
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Escape');
  await expect(menu).toBeHidden();
  await expect(dialog).toBeVisible();

  // The NEXT Escape is unconsumed and may reach the modal.
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();

  // Round 2: outside-click. Re-open modal + menu, mousedown on modal
  // content → menu closes, modal stays.
  await page
    .getByRole('link', { name: new RegExp(JOB_TITLE, 'i') })
    .or(page.getByText(JOB_TITLE).first())
    .first()
    .click();
  await expect(dialog).toBeVisible();
  await dialog
    .getByRole('button', { name: /add to target/i })
    .first()
    .click();
  await expect(menu).toBeVisible();

  await dialog.getByRole('heading', { name: JOB_TITLE }).click();
  await expect(menu).toBeHidden();
  await expect(dialog).toBeVisible();
});
