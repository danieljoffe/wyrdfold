/* eslint-disable no-console */
import { chromium } from '@playwright/test';

const b = await chromium.launch();
const p = await (
  await b.newContext({ storageState: './stress-results/auth.json', baseURL: 'https://wyrdfold.com' })
).newPage();

const body = await (await p.request.get('/api/targets/mine')).json();
const rows = (body.targets ?? []).map(t => ({
  ut: t.user_target?.id,
  tid: t.user_target?.target_id,
  active: t.user_target?.is_active,
  label: t.label ?? t.target?.label ?? t.normalized_label ?? '(no label field)',
}));
console.log('TARGETS:', JSON.stringify(rows, null, 1));

const scratch = rows.filter(r => /E2E Coverage Scratch/i.test(String(r.label)));
console.log('\nscratch targets found:', scratch.length);
for (const s of scratch) {
  for (const [what, id] of [['user_target', s.ut], ['target', s.tid]]) {
    if (!id) continue;
    const r = await p.request.delete(`/api/targets/${id}`);
    console.log(`  DELETE /api/targets/${id} (${what}) -> ${r.status()}`);
    if (r.ok()) break;
  }
}
const after = await (await p.request.get('/api/targets/mine')).json();
const left = (after.targets ?? []).filter(t =>
  /E2E Coverage Scratch/i.test(JSON.stringify(t))
).length;
console.log('\nscratch remaining after cleanup:', left);
console.log('total targets now:', (after.targets ?? []).length);
await b.close();
