import { axe, toHaveNoViolations } from 'jest-axe';

// jest-axe re-exports axe-core's run options as `axe.RunOptions`, but
// the published types don't surface it as a top-level export — narrow
// what we need locally.
type RunOptions = Parameters<typeof axe>[1];

// One-time global wireup. Each spec that wants axe assertions imports
// `expectNoA11yViolations` from this module — the side-effect call to
// expect.extend runs at import time so individual specs no longer need
// the boilerplate (#25 F1).
expect.extend(toHaveNoViolations);

interface ExpectNoA11yViolationsOptions {
  /**
   * Rules to disable for this specific call. Use only for violations
   * known to originate in third-party code we can't easily fix from
   * this repo (e.g. `@danieljoffe/shared-ui` components whose internal
   * markup we don't own). Add a TODO + tracking link in the spec when
   * you disable a rule.
   */
  disableRules?: readonly string[];
}

/**
 * Run axe-core against `container` and assert zero violations. Standard
 * shape across all component specs so a new spec just does:
 *
 *     it('has no accessibility violations', async () => {
 *       const { container } = render(<MyThing />);
 *       await expectNoA11yViolations(container);
 *     });
 *
 * Centralized so we can tweak rules / impact thresholds in one place
 * later without sweeping every spec.
 */
export async function expectNoA11yViolations(
  container: Element,
  options: ExpectNoA11yViolationsOptions = {}
): Promise<void> {
  const runOptions: RunOptions = options.disableRules
    ? {
        rules: Object.fromEntries(
          options.disableRules.map(id => [id, { enabled: false }])
        ),
      }
    : {};
  const results = await axe(container, runOptions);
  expect(results).toHaveNoViolations();
}
