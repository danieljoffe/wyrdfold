import { test as base, expect, type Page } from '@playwright/test';

/**
 * Shared test fixture with the failure tripwires the 2026-08-05 prod drive
 * showed the suite lacked (#608): every spec was green while every
 * client-side navigation 503'd at the edge and fell back to full page
 * loads, because nothing ever looked at a status code.
 *
 * 1. Same-origin 5xx tripwire — any response ≥500 from the app under test
 *    fails the spec, no assertions required. This catches both the RSC
 *    flight 503s (#601) and BFF/API 5xxs (#604) in every existing spec.
 * 2. Page-error tripwire — uncaught exceptions in the page fail the spec.
 *    (Console *messages* are not asserted on; only real thrown errors.)
 *
 * Escape hatch: a spec that intentionally provokes a 5xx calls
 * ``allow5xx(/url pattern/)`` before triggering it.
 */
const ALLOW_5XX = Symbol('allow5xx');

type PageWithAllowlist = Page & { [ALLOW_5XX]?: RegExp[] };

type TripwireFixtures = {
  allow5xx: (pattern: RegExp) => void;
};

export const test = base.extend<TripwireFixtures>({
  // Depends on ``page`` so the allowlist array below always exists first.
  allow5xx: async ({ page }, use) => {
    await use((pattern: RegExp) => {
      (page as PageWithAllowlist)[ALLOW_5XX]?.push(pattern);
    });
  },

  page: async ({ page, baseURL }, use, testInfo) => {
    const allowlist: RegExp[] = [];
    (page as PageWithAllowlist)[ALLOW_5XX] = allowlist;

    const serverErrors: string[] = [];
    const pageErrors: string[] = [];
    const origin = baseURL ? new URL(baseURL).origin : null;

    page.on('response', response => {
      const url = response.url();
      if (response.status() < 500) return;
      if (origin && !url.startsWith(origin)) return;
      if (allowlist.some(p => p.test(url))) return;
      serverErrors.push(
        `${response.status()} ${response.request().method()} ${url}`
      );
    });

    page.on('pageerror', error => {
      pageErrors.push(error.message);
    });

    await use(page);

    // Thrown after the spec body so the failure names every offender.
    // testInfo guards keep a spec's own failure as the primary report.
    if (testInfo.status === 'passed' && serverErrors.length > 0) {
      throw new Error(
        `Same-origin 5xx responses during this spec (tripwire #608):\n  ${serverErrors.join('\n  ')}`
      );
    }
    if (testInfo.status === 'passed' && pageErrors.length > 0) {
      throw new Error(
        `Uncaught page errors during this spec (tripwire #608):\n  ${pageErrors.join('\n  ')}`
      );
    }
  },
});

export { expect };
