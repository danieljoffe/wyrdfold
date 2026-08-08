/* eslint-disable playwright/no-networkidle, playwright/no-wait-for-timeout, @typescript-eslint/no-empty-function --
 * Recon script, not a spec: it deliberately waits for the network to settle
 * before dumping control names, and swallows settle timeouts. */
/**
 * Selector reconnaissance for the coverage sweep. Dumps the accessible
 * name of every interactive control on each authed surface so the specs
 * can target real names instead of guesses (the 2026-08-07 run lost four
 * actions to invented selectors, each eating a 45s timeout).
 *
 *   node src/stress/probe-controls.mjs [pathOverride ...]
 */
import { chromium } from '@playwright/test';

const browser = await chromium.launch();
const ctx = await browser.newContext({
  storageState: './stress-results/auth.json',
  baseURL: 'https://wyrdfold.com',
});
const page = await ctx.newPage();

async function jobIds() {
  const res = await page.request.get(
    'https://wyrdfold.com/api/jobs?page_size=5&status=resume_draft'
  );
  const body = await res.json();
  return (body.postings ?? []).map(p => p.id);
}
async function targetIds() {
  const res = await page.request.get('https://wyrdfold.com/api/targets/mine');
  const body = await res.json();
  return (body.targets ?? []).map(t => ({
    id: t.id ?? t.target_id,
    label: t.label ?? t.normalized_label,
  }));
}

const jobs = await jobIds();
const targets = await targetIds();
console.log('JOB IDS:', jobs.join(', '));
console.log('TARGETS:', JSON.stringify(targets));

const paths = process.argv.slice(2).length
  ? process.argv.slice(2)
  : [
      '/targets',
      targets[0] ? `/targets/${targets[0].id}` : null,
      '/profile',
      '/settings',
      '/onboarding',
      jobs[0] ? `/jobs/${jobs[0]}` : null,
      jobs[0] ? `/jobs/${jobs[0]}/resume` : null,
      jobs[0] ? `/jobs/${jobs[0]}/cover-letter` : null,
    ].filter(Boolean);

for (const path of paths) {
  console.log(`\n\n########## ${path} ##########`);
  try {
    await page.goto(path, { waitUntil: 'domcontentloaded', timeout: 90_000 });
    await page.waitForLoadState('networkidle', { timeout: 45_000 }).catch(() => {});
    await page.waitForTimeout(1500);
  } catch (e) {
    console.log('NAV FAILED:', String(e).slice(0, 160));
    continue;
  }
  for (const role of ['button', 'link', 'tab', 'checkbox', 'combobox', 'textbox', 'slider', 'radio', 'switch']) {
    const names = await page
      .getByRole(role)
      .evaluateAll(els =>
        els
          .map(e => (e.getAttribute('aria-label') || e.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 52))
          .filter(Boolean)
      )
      .catch(() => []);
    const uniq = [...new Set(names)];
    if (uniq.length) console.log(`  ${role}: ${JSON.stringify(uniq)}`);
  }
  const heads = await page
    .locator('h1,h2,h3')
    .evaluateAll(els => [...new Set(els.map(e => (e.textContent || '').trim().slice(0, 46)).filter(Boolean))])
    .catch(() => []);
  console.log(`  headings: ${JSON.stringify(heads)}`);
}

await browser.close();
