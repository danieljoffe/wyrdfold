import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';
import LlmUsageCard, {
  rollOffIsInformative,
  usageVariant,
} from '../LlmUsageCard';

// The monthly-allowance meter moved from a hand-rolled bar to the shared-ui
// ProgressBar. These lock the two things that migration could silently break:
// the colour tiers, and the zero-limit guard (ProgressBar reads 100% at max=0,
// but a no-limit account must show an EMPTY bar like the old meter did).

describe('usageVariant — the meter colour tiers', () => {
  it('is accent (brand) below 70%', () => {
    expect(usageVariant(0, 10)).toBe('accent');
    expect(usageVariant(6.9, 10)).toBe('accent');
  });

  it('turns warning (amber) from exactly 70% up to <90%', () => {
    expect(usageVariant(7, 10)).toBe('warning'); // boundary
    expect(usageVariant(8.9, 10)).toBe('warning');
  });

  it('turns error (red) at exactly 90% and above, including over-limit', () => {
    expect(usageVariant(9, 10)).toBe('error'); // boundary
    expect(usageVariant(12, 10)).toBe('error'); // spent past the limit
  });

  it('treats a zero/absent limit as 0% → accent (no divide-by-zero)', () => {
    expect(usageVariant(5, 0)).toBe('accent');
  });
});

describe('rollOffIsInformative — the roll-off line gate (re-sweep R5)', () => {
  const now = new Date('2026-08-13T12:00:00Z');

  it('hides a date within the next day — perpetually "today" for an active account', () => {
    expect(rollOffIsInformative('2026-08-13T13:00:00Z', now)).toBe(false);
    expect(rollOffIsInformative('2026-08-14T11:00:00Z', now)).toBe(false);
  });

  it('shows a date more than a day out (usage paused → real information)', () => {
    expect(rollOffIsInformative('2026-08-20T12:00:00Z', now)).toBe(true);
  });

  it('hides on malformed input', () => {
    expect(rollOffIsInformative('not a date', now)).toBe(false);
  });
});

const originalFetch = global.fetch;

function mockUsage(monthly: { spent_usd: number; limit_usd: number }) {
  global.fetch = jest.fn().mockResolvedValue({
    ok: true,
    json: async () => ({
      hourly: { spent_usd: 0, limit_usd: 1 },
      daily: { spent_usd: 0, limit_usd: 1 },
      monthly,
      monthly_resets_at: null,
      analysis_daily_used: 0,
      analysis_daily_limit: 5,
    }),
  } as Response) as unknown as typeof fetch;
}

afterEach(() => {
  global.fetch = originalFetch;
  jest.clearAllMocks();
});

describe('LlmUsageCard monthly allowance meter', () => {
  it('reflects the monthly spend as a percentage of the limit', async () => {
    mockUsage({ spent_usd: 5, limit_usd: 10 });
    render(<LlmUsageCard />);
    const bar = await screen.findByRole('progressbar', {
      name: /30-day allowance used/i,
    });
    expect(bar).toHaveAttribute('aria-valuenow', '50');
  });

  it('pins a zero-limit (no-limit) account to an EMPTY bar, not a full one', async () => {
    // Regression guard: value/max reads 100% at max=0. The guard passes
    // value=0 / max=100 so a no-limit account shows 0%, as the old meter did —
    // without it this bar would read 100.
    mockUsage({ spent_usd: 5, limit_usd: 0 });
    render(<LlmUsageCard />);
    const bar = await screen.findByRole('progressbar', {
      name: /30-day allowance used/i,
    });
    expect(bar).toHaveAttribute('aria-valuenow', '0');
  });
});
