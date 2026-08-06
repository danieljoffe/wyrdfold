import fs from 'node:fs';
import path from 'node:path';
import type { Page, Request } from '@playwright/test';

/**
 * Timed-action harness for the full-app stress sweep.
 *
 * Every user-visible action runs through ``timedAction``: it brackets the
 * interaction (act → success assertion satisfied), captures every network
 * request that completes inside the bracket via Playwright's own network
 * layer (trustworthy, unlike devtools-extension status columns), and appends
 * one JSONL row per action to the run ledger. Anything over FLAG_MS is a
 * diagnostics flag — the correlation pass joins those with the API's
 * server-side ``slow_request`` lines to split browser time into
 * edge/BFF vs API vs DB/LLM shares.
 */

export const FLAG_MS = 300;

export interface NetTrace {
  url: string;
  method: string;
  status: number;
  /** requestStart → responseEnd, ms (Playwright request.timing()). */
  durationMs: number;
  /** ms offset of request start relative to the action start — makes
   *  serial waterfalls visible as a staircase in the trace. */
  startOffsetMs: number;
}

export interface ActionRow {
  id: string;
  surface: string;
  /** Interaction time: act() start → success assertion satisfied. */
  elapsedMs: number;
  flagged: boolean;
  pass: boolean;
  error?: string;
  net: NetTrace[];
  startedAt: string;
}

const LEDGER_DIR =
  process.env['STRESS_LEDGER_DIR'] ??
  path.join(process.cwd(), 'stress-results');
const LEDGER = path.join(LEDGER_DIR, 'ledger.jsonl');

export function ledgerPath(): string {
  return LEDGER;
}

export function resetLedger(): void {
  fs.mkdirSync(LEDGER_DIR, { recursive: true });
  fs.writeFileSync(LEDGER, '');
}

export function readLedger(): ActionRow[] {
  if (!fs.existsSync(LEDGER)) return [];
  return fs
    .readFileSync(LEDGER, 'utf8')
    .split('\n')
    .filter(Boolean)
    .map(l => JSON.parse(l) as ActionRow);
}

function appendRow(row: ActionRow): void {
  fs.mkdirSync(LEDGER_DIR, { recursive: true });
  fs.appendFileSync(LEDGER, `${JSON.stringify(row)}\n`);
}

/** Only same-app traffic counts toward an action's network trace. */
function isAppRequest(url: string): boolean {
  return (
    url.includes('/api/') ||
    url.includes('/_next/data') ||
    url.includes('_rsc=') ||
    url.includes('supabase.co/auth')
  );
}

/**
 * Run one manifest action: attach network listeners, act, await the
 * success assertion, and ledger the result. Failures are recorded (pass:
 * false) and re-thrown only when ``hardFail`` — the sweep default is to
 * keep going so one broken action doesn't hide the rest of the timings;
 * the coverage gate surfaces every failure at the end.
 */
export async function timedAction(
  page: Page,
  id: string,
  surface: string,
  act: () => Promise<void>,
  assertDone: () => Promise<void>,
  opts: { hardFail?: boolean; deadlineMs?: number } = {}
): Promise<ActionRow> {
  const net: NetTrace[] = [];
  const t0 = Date.now();
  const onFinished = (req: Request) => {
    const url = req.url();
    if (!isAppRequest(url)) return;
    const timing = req.timing();
    const resp = req.response();
    void resp?.then(r => {
      const durationMs =
        timing.responseEnd > 0 && timing.requestStart >= 0
          ? timing.responseEnd - timing.requestStart
          : -1;
      net.push({
        url: url.replace(/^https?:\/\/[^/]+/, ''),
        method: req.method(),
        status: r?.status() ?? 0,
        durationMs: Math.round(durationMs * 10) / 10,
        startOffsetMs: Math.max(0, Math.round(timing.startTime - t0)),
      });
    });
  };
  page.on('requestfinished', onFinished);
  page.on('requestfailed', req => {
    if (isAppRequest(req.url())) {
      net.push({
        url: req.url().replace(/^https?:\/\/[^/]+/, ''),
        method: req.method(),
        status: -1,
        durationMs: -1,
        startOffsetMs: Math.max(0, Math.round(Date.now() - t0)),
      });
    }
  });

  let pass = true;
  let error: string | undefined;
  const startedAt = new Date().toISOString();
  // Per-action deadline: one stuck locator must fail THIS row, not starve
  // every action behind it in the serial sweep (the first run lost 6 of 7
  // public actions to a single bad selector eating the test budget).
  const deadlineMs = opts.deadlineMs ?? 60_000;
  const deadline = new Promise<never>((_, reject) =>
    setTimeout(
      () => reject(new Error(`action deadline ${deadlineMs}ms exceeded`)),
      deadlineMs
    )
  );
  try {
    await Promise.race([
      (async () => {
        await act();
        await assertDone();
      })(),
      deadline,
    ]);
  } catch (e) {
    pass = false;
    error = e instanceof Error ? e.message.slice(0, 300) : String(e);
    if (opts.hardFail) {
      page.off('requestfinished', onFinished);
      throw e;
    }
  }
  const elapsedMs = Date.now() - t0;
  // Small settle so late responses land in the trace before we detach.
  // Plain timer, NOT page.waitForTimeout: a page wedged mid-navigation
  // blocks page-bound waits and killed a whole test past its budget.
  await new Promise(resolve => setTimeout(resolve, 120));
  page.off('requestfinished', onFinished);

  const row: ActionRow = {
    id,
    surface,
    elapsedMs,
    flagged: elapsedMs > FLAG_MS,
    pass,
    ...(error ? { error } : {}),
    net,
    startedAt,
  };
  appendRow(row);
  return row;
}
