import AxeBuilder from '@axe-core/playwright';
import { test, expect } from '@playwright/test';

// Integration-level accessibility checks (#25 F6). Unit-level jest-axe
// (see test-utils/axe.ts) catches violations on isolated components;
// these run against the actual rendered page so violations introduced
// by layout composition, hydrated state, or middleware redirects get
// caught too.
//
// Scope: just the un-authed surfaces the CI suite already exercises.
// Real authed pages stay local-only with the rest of `authed-*.spec.ts`.
//
// Rule disables (shared across pages):
// - `heading-order` — shared-ui Alert renders its `title` as an <h5>
//   which trips heading-order on /login. Mirrors the same disable in
//   MagicLinkForm.spec.tsx (#61). The fix lives in
//   @danieljoffe/shared-ui — re-enable once that ships.
const SHARED_DISABLES = ['heading-order'];

// Landing-page disables now match SHARED_DISABLES — the previous
// `color-contrast` and `link-in-text-block` violations were fixed
// (Beta badges flipped to `text-brand-950`; footer "Built by" link
// picked up `underline underline-offset-2`).
const LANDING_DISABLES = SHARED_DISABLES;

test.describe('Accessibility (public pages)', () => {
  test('/login has no serious or critical axe violations', async ({ page }) => {
    await page.goto('/login');
    await page.getByRole('heading', { name: 'Sign in', level: 1 }).waitFor();

    const results = await new AxeBuilder({ page })
      .disableRules(SHARED_DISABLES)
      .analyze();

    const blocking = results.violations.filter(
      v => v.impact === 'serious' || v.impact === 'critical'
    );
    expect(blocking, formatViolations(blocking)).toEqual([]);
  });

  test('/ landing page has no serious or critical axe violations', async ({
    page,
  }) => {
    await page.goto('/');

    const results = await new AxeBuilder({ page })
      .disableRules(LANDING_DISABLES)
      .analyze();

    const blocking = results.violations.filter(
      v => v.impact === 'serious' || v.impact === 'critical'
    );
    expect(blocking, formatViolations(blocking)).toEqual([]);
  });
});

// Build a one-line-per-violation summary so a failing assertion's
// message says which rules tripped — `toEqual([])` on its own dumps the
// whole violation tree, which is unreadable in CI logs.
function formatViolations(
  violations: Array<{
    id: string;
    impact?: string | null;
    nodes: ReadonlyArray<unknown>;
  }>
): string {
  if (violations.length === 0) return 'no violations';
  return violations
    .map(v => `${v.impact}: ${v.id} (${v.nodes.length} node(s))`)
    .join('\n');
}
