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

// Landing-page-specific disables (TODO: tackle as a focused PR):
// - `color-contrast` — the chartreuse brand-300 used for the "Beta"
//   badge + similar soft pills has 1.71:1 contrast on near-white
//   backgrounds (needs 4.5:1 for AA). Real launch-blocker; tracked as
//   a follow-up so this spec can still guard against new violations.
// - `link-in-text-block` — one "Built by Daniel Joffe" link in the
//   public footer carries no underline distinct from surrounding text.
//   Fix: add underline-on-hover-only OR a 3:1+ contrast difference.
const LANDING_DISABLES = [
  ...SHARED_DISABLES,
  'color-contrast',
  'link-in-text-block',
];

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
