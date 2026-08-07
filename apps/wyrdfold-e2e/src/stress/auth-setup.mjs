/**
 * Prod auth bootstrap for the stress sweep: consumes a pre-minted magic
 * link (MAGIC_LINK env — minted via the prod service role out-of-band),
 * lands authenticated, saves the Playwright storage state, and ledgers
 * the timed 'auth.magic-link.consume' action like any other manifest row.
 *
 *   MAGIC_LINK='https://wyrdfold.com/auth/callback?...' node src/stress/auth-setup.mjs
 */
import fs from 'node:fs';
import path from 'node:path';
import { chromium } from '@playwright/test';

const link = process.env.MAGIC_LINK;
if (!link) throw new Error('MAGIC_LINK env required');
const outDir = process.env.STRESS_LEDGER_DIR ?? './stress-results';
fs.mkdirSync(outDir, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage();
const t0 = Date.now();
await page.goto(link);
await page.waitForURL(/dashboard|jobs|onboarding/, { timeout: 60_000 });
const elapsedMs = Date.now() - t0;
await page.context().storageState({ path: path.join(outDir, 'auth.json') });
fs.appendFileSync(
  path.join(outDir, 'ledger.jsonl'),
  `${JSON.stringify({
    id: 'auth.magic-link.consume',
    surface: 'auth',
    elapsedMs,
    flagged: elapsedMs > 300,
    pass: true,
    net: [],
    startedAt: new Date(t0).toISOString(),
  })}\n`
);
console.log(`authed in ${elapsedMs}ms; storage state saved`);
await browser.close();
