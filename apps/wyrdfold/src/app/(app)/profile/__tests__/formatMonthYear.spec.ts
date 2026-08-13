import { formatDateRange, formatMonthYear } from '../ProfilePage';

// No TZ pinning here (runtime process.env.TZ is unreliable across
// platforms — see localDateStamp.spec.ts): these hold in EVERY zone
// precisely because the implementation never constructs a Date. The old
// Date-based implementation fails them on any machine west of UTC.

describe('formatMonthYear', () => {
  // The exact prod repro: every Experience card rendered one month before
  // the stored value ("2024-01" showed as "Dec 2023" in Los Angeles) while
  // the résumé printed the stored month. These fail under the old
  // Date-based implementation with TZ=America/Los_Angeles.
  it('renders the STORED month, independent of timezone', () => {
    expect(formatMonthYear('2024-01')).toBe('Jan 2024');
    expect(formatMonthYear('2015-05')).toBe('May 2015');
    expect(formatMonthYear('2019-08')).toBe('Aug 2019');
  });

  it('accepts full dates, formatting month-year only', () => {
    expect(formatMonthYear('2023-12-01')).toBe('Dec 2023');
  });

  it('passes through non-matching input untouched', () => {
    expect(formatMonthYear('2024')).toBe('2024');
    expect(formatMonthYear('circa 2020')).toBe('circa 2020');
    expect(formatMonthYear('')).toBe('');
    expect(formatMonthYear('2024-13')).toBe('2024-13');
    expect(formatMonthYear('2024-00')).toBe('2024-00');
  });
});

describe('formatDateRange', () => {
  it('renders an en-dash range, with Present for open roles', () => {
    expect(formatDateRange('2021-01', '2023-01')).toBe('Jan 2021 – Jan 2023');
    expect(formatDateRange('2024-01', null)).toBe('Jan 2024 – Present');
  });
});
