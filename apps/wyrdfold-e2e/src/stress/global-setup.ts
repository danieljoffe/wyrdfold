import { resetLedger } from './timing';

/**
 * Truncate the action ledger once per sweep.
 *
 * ``timing.ts`` has always exported ``resetLedger`` but nothing ever called
 * it, so the JSONL ledger was append-only across runs — and the coverage gate
 * reads that ledger. A row written by *a previous* sweep therefore counted as
 * "executed" in *this* one, which is precisely the aspirational-vs-measured
 * coverage the manifest docstring warns about: an action could rot for weeks
 * and the gate would stay green.
 *
 * Set STRESS_KEEP_LEDGER=1 to append instead (useful when re-running a single
 * project against an otherwise complete ledger).
 */
export default function globalSetup(): void {
  if (process.env['STRESS_KEEP_LEDGER']) return;
  resetLedger();
}
