// TZ must be pinned BEFORE the module (or any Date) is touched — CI runs in
// UTC, where the old `toISOString().slice(0, 10)` bug is invisible.
process.env.TZ = 'America/Los_Angeles';

import { localDateStamp } from '../localDateStamp';

describe('localDateStamp', () => {
  // The exact prod repro (ux-sweep 2026-08-12 §A2): a résumé generated on
  // the evening of Aug 12 PT was filenamed …-2026-08-13. 02:00 UTC on the
  // 13th IS the 12th in Los Angeles — the UTC slice returned '2026-08-13'.
  it("uses the user's local date, not the UTC date", () => {
    expect(localDateStamp(new Date('2026-08-13T02:00:00Z'))).toBe('2026-08-12');
  });

  it('matches UTC when local and UTC agree', () => {
    expect(localDateStamp(new Date('2026-08-12T18:00:00Z'))).toBe('2026-08-12');
  });

  it('zero-pads month and day', () => {
    expect(localDateStamp(new Date('2026-01-05T20:00:00Z'))).toBe('2026-01-05');
  });
});
