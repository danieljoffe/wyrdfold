/**
 * Ledger reporter: prints the coverage roll-up and the >300ms flag list for a
 * sweep. Reads stress-results/ledger.jsonl (written by timing.ts).
 *
 *   node src/stress/report.mjs                 # full roll-up
 *   node src/stress/report.mjs 'jobs\.'        # only ids matching a regex
 */
import fs from 'node:fs';
import { MANIFEST } from './manifest.ts';

const ESC = String.fromCharCode(27);
const strip = s => String(s ?? '').split(`${ESC  }[`).join('|').replace(/\|[0-9;]*m/g, '');

const rows = fs
  .readFileSync('stress-results/ledger.jsonl', 'utf8')
  .split('\n')
  .filter(Boolean)
  .map(l => JSON.parse(l));

const filter = process.argv[2] ? new RegExp(process.argv[2]) : null;
const sel = filter ? rows.filter(r => filter.test(r.id)) : rows;

if (filter) {
  for (const r of sel) {
    console.log(
      `${(r.pass ? 'PASS ' : 'FAIL ') + String(r.elapsedMs).padStart(7)  }ms  ${  r.id}`
    );
    if (!r.pass) {
      console.log(`      ${  strip(r.error).split('\n').slice(0, 3).join(' | ').slice(0, 300)}`);
    }
  }
  process.exit(0);
}

const executed = new Set(rows.map(r => r.id));
const excluded = MANIFEST.filter(m => m.excluded);
const missing = MANIFEST.filter(m => !m.excluded && !executed.has(m.id)).map(m => m.id);
const unknown = [...executed].filter(id => !MANIFEST.some(m => m.id === id));
const failures = rows.filter(r => !r.pass);
const flagged = rows.filter(r => r.flagged).sort((a, b) => b.elapsedMs - a.elapsedMs);

console.log('=== COVERAGE ===');
console.log(`manifest      ${MANIFEST.length}`);
console.log(`executed      ${executed.size}`);
console.log(`excluded      ${excluded.length}`);
console.log(`missing       ${missing.length}${missing.length ? `: ${  missing.join(', ')}` : ''}`);
console.log(`unknown ids   ${unknown.length}${unknown.length ? `: ${  unknown.join(', ')}` : ''}`);

console.log(`\n=== FAILURES (${failures.length}) ===`);
for (const r of failures) {
  console.log(`  ${r.id}`);
  console.log(`      ${  strip(r.error).split('\n').slice(0, 2).join(' | ').slice(0, 240)}`);
}

console.log(`\n=== OVER 300ms (${flagged.length} of ${rows.length}) ===`);
for (const r of flagged) {
  const worst = [...(r.net ?? [])].sort((a, b) => b.durationMs - a.durationMs)[0];
  const tail = worst ? `   slowest req ${Math.round(worst.durationMs)}ms ${worst.status} ${worst.url.split('?')[0]}` : '';
  console.log(`  ${String(r.elapsedMs).padStart(7)}ms  ${r.id.padEnd(42)}${tail}`);
}

const bySurface = {};
for (const r of rows) {
  bySurface[r.surface] ??= { n: 0, flagged: 0, fail: 0, total: 0 };
  bySurface[r.surface].n++;
  bySurface[r.surface].total += r.elapsedMs;
  if (r.flagged) bySurface[r.surface].flagged++;
  if (!r.pass) bySurface[r.surface].fail++;
}
console.log('\n=== BY SURFACE ===');
for (const [s, v] of Object.entries(bySurface).sort((a, b) => b[1].flagged - a[1].flagged)) {
  console.log(
    `  ${s.padEnd(11)} actions ${String(v.n).padStart(3)}   >300ms ${String(v.flagged).padStart(3)}   failed ${String(v.fail).padStart(2)}   median ${Math.round(v.total / v.n)}ms`
  );
}
