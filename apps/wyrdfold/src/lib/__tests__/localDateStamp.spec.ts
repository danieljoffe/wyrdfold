import { localDateStamp } from '../localDateStamp';

// Deliberately TZ-agnostic: runtime `process.env.TZ` pinning is unreliable
// (honored on macOS, ignored on CI's Linux workers once the zone is cached —
// observed on PR #710's first CI run). Dates are constructed from LOCAL
// components, so these assertions hold in any zone; on any non-UTC machine
// (e.g. dev laptops) the evening case also catches a regression to
// `toISOString().slice(0, 10)`, which was the §A2 bug.

describe('localDateStamp', () => {
  it('stamps the local calendar date', () => {
    expect(localDateStamp(new Date(2026, 7, 12, 9, 30))).toBe('2026-08-12');
  });

  it('stays on the local date late in the evening (the §A2 repro shape)', () => {
    // 23:30 local on Aug 12 is already Aug 13 UTC anywhere west of UTC-0:30 —
    // the old UTC slice stamped tomorrow's date for these sessions.
    expect(localDateStamp(new Date(2026, 7, 12, 23, 30))).toBe('2026-08-12');
  });

  it('zero-pads month and day', () => {
    expect(localDateStamp(new Date(2026, 0, 5, 12, 0))).toBe('2026-01-05');
  });
});
