import { chromium } from '@playwright/test';

const browser = await chromium.launch();
const ctx = await browser.newContext({
  storageState: './stress-results/auth.json',
  baseURL: 'https://wyrdfold.com',
});
const page = await ctx.newPage();
await page.goto('https://wyrdfold.com/jobs', { waitUntil: 'domcontentloaded', timeout: 90_000 });
await page
  .locator('[aria-label^="Match score"]')
  .first()
  .waitFor({ timeout: 60_000 });

// 1. Filter pills row.
console.log('=== filter bar buttons ===');
for (const b of await page.getByRole('button').all()) {
  const name = await b.getAttribute('aria-label').catch(() => null);
  const text = (await b.textContent().catch(() => '')) ?? '';
  const t = (name ?? text).trim().slice(0, 40);
  if (t) console.log(`button: "${t}"`);
}

// 2. Open the min-score pill, dump what appears.
await page.getByRole('button', { name: /any score/i }).click();
await page.waitForTimeout(600);
console.log('=== after opening Any score ===');
console.log(
  await page
    .locator('[role="menu"], [role="listbox"], [role="option"], [role="menuitem"]')
    .evaluateAll(els =>
      els.map(e => `${e.getAttribute('role')}: "${(e.textContent ?? '').trim().slice(0, 30)}"`)
    )
);
const snapshot = await page.locator('body').ariaSnapshot();
const idx = snapshot.indexOf('Any score');
console.log('=== aria snapshot around score pill ===');
console.log(snapshot.slice(Math.max(0, idx - 300), idx + 900));

// 3. Expand a panel via company cell; dump panel region roles.
await page.keyboard.press('Escape');
await page.locator('tbody tr td:nth-child(5)').first().click();
await page.waitForTimeout(2500);
console.log('=== panel container candidates ===');
console.log(
  await page
    .locator('div.bg-surface-tertiary')
    .evaluateAll(els => els.map(e => e.className.slice(0, 60)))
);
const snap2 = await page.locator('body').ariaSnapshot();
const i2 = snap2.indexOf('Score Breakdown');
console.log('=== aria snapshot around panel ===');
console.log(snap2.slice(Math.max(0, i2 - 1200), i2 + 400));

await browser.close();
