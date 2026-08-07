import { defineConfig } from '@playwright/test';

/**
 * Stress-sweep config — deliberately separate from the CI e2e config:
 * prod baseURL, ONE worker (sequential timings must not contend with each
 * other), zero retries (a retried timing is a lie), generous timeouts
 * (LLM rows run minutes). Auth comes from a pre-minted storage state
 * (scripts/stress-auth-setup mints it via a prod magic link).
 */
export default defineConfig({
  testDir: './src/stress',
  timeout: 900_000,
  retries: 0,
  workers: 1,
  fullyParallel: false,
  reporter: [['list']],
  use: {
    baseURL: process.env['STRESS_BASE_URL'] ?? 'https://wyrdfold.com',
    trace: 'off',
    video: 'off',
    // Under the runner these default to 0 (unbounded) — a hanging RSC
    // stream then wedges goto() forever and eats the test budget. Bound
    // them so a hang surfaces as a timed failure row instead.
    navigationTimeout: 60_000,
    actionTimeout: 45_000,
  },
  projects: [
    {
      name: 'public',
      testMatch: /stress-public\.spec\.ts/,
    },
    {
      name: 'authed',
      testMatch: /stress-authed\.spec\.ts/,
      dependencies: ['public'],
      use: {
        storageState:
          process.env['STRESS_STORAGE_STATE'] ?? './stress-results/auth.json',
      },
    },
    {
      name: 'gate',
      testMatch: /zz-coverage-gate\.spec\.ts/,
      dependencies: ['authed'],
    },
  ],
});
