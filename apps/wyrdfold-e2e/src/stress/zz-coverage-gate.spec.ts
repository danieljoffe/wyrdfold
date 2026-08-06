import fs from 'node:fs';
import path from 'node:path';
import { test, expect } from '@playwright/test';
import { MANIFEST } from './manifest';
import { FLAG_MS, readLedger } from './timing';

/**
 * The 100%-coverage gate + flag report. Runs LAST (zz- prefix, single
 * worker): every manifest id must be either executed (ledger row) or
 * excluded-with-reason. Also writes flags.json (everything > FLAG_MS,
 * slowest first) and coverage.json for the correlation pass.
 */

test('coverage gate: every manifest action executed or excluded', () => {
  const rows = readLedger();
  const executed = new Set(rows.map(r => r.id));
  const missing = MANIFEST.filter(m => !m.excluded && !executed.has(m.id)).map(
    m => m.id
  );
  const unknown = [...executed].filter(id => !MANIFEST.some(m => m.id === id));
  const failures = rows.filter(r => !r.pass).map(r => `${r.id}: ${r.error}`);
  const flags = rows
    .filter(r => r.flagged)
    .sort((a, b) => b.elapsedMs - a.elapsedMs);

  const outDir = path.dirname(
    process.env['STRESS_LEDGER_DIR'] ??
      path.join(process.cwd(), 'stress-results', 'x')
  );
  fs.mkdirSync(outDir, { recursive: true });
  fs.writeFileSync(
    path.join(outDir, 'flags.json'),
    JSON.stringify(flags, null, 2)
  );
  fs.writeFileSync(
    path.join(outDir, 'coverage.json'),
    JSON.stringify(
      {
        flagThresholdMs: FLAG_MS,
        manifestTotal: MANIFEST.length,
        executed: executed.size,
        excluded: MANIFEST.filter(m => m.excluded).length,
        missing,
        unknown,
        failures,
        flaggedCount: flags.length,
      },
      null,
      2
    )
  );

  expect(unknown, `ledger rows not in manifest: ${unknown.join(', ')}`).toEqual(
    []
  );
  expect(
    missing,
    `manifest actions never executed: ${missing.join(', ')}`
  ).toEqual([]);
});
