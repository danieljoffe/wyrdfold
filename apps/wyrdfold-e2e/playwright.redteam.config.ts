import { defineConfig } from '@playwright/test';

/**
 * Adversarial (red-team) sweep — deliberately SEPARATE from
 * `playwright.stress.config.ts`.
 *
 * Two reasons it cannot share that config:
 *
 *  1. THE COVERAGE GATE. `zz-coverage-gate.spec.ts` asserts that every ledger
 *     row maps to a MANIFEST id (`unknown` must be empty). Red-team ids are
 *     not user journeys and do not belong in the coverage manifest, so writing
 *     them into the same ledger would fail the gate for the wrong reason.
 *     This config therefore defaults `STRESS_LEDGER_DIR` to its own directory.
 *
 *  2. DIFFERENT QUESTION. The stress sweep measures the app doing its job; this
 *     one asks whether the app can be made to crash, hang, or render nothing.
 *     Keeping them apart keeps each report readable.
 *
 * Auth reuses the stress sweep's pre-minted storage state.
 */
process.env['STRESS_LEDGER_DIR'] ??= './redteam-results';

export default defineConfig({
  testDir: './src/stress',
  testMatch: /redteam-.*\.spec\.ts/,
  timeout: 900_000,
  retries: 0,
  workers: 1,
  fullyParallel: false,
  reporter: [['list']],
  use: {
    baseURL: process.env['STRESS_BASE_URL'] ?? 'https://wyrdfold.com',
    trace: 'off',
    video: 'off',
    navigationTimeout: 60_000,
    actionTimeout: 45_000,
    storageState:
      process.env['STRESS_STORAGE_STATE'] ?? './stress-results/auth.json',
  },
});
