import { timeAgo } from './timeAgo';

describe('timeAgo', () => {
  const NOW = new Date('2026-07-09T12:00:00Z').getTime();

  beforeEach(() => {
    jest.spyOn(Date, 'now').mockReturnValue(NOW);
  });
  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('returns an em dash for a null date', () => {
    expect(timeAgo(null)).toBe('—');
  });

  it('returns "today" within the first day', () => {
    expect(timeAgo(new Date(NOW).toISOString())).toBe('today');
    expect(timeAgo(new Date(NOW - 3600_000).toISOString())).toBe('today');
    expect(timeAgo(new Date(NOW - 23 * 3600_000).toISOString())).toBe('today');
  });

  it('returns "1d ago" at exactly one day', () => {
    expect(timeAgo(new Date(NOW - 86_400_000).toISOString())).toBe('1d ago');
  });

  it('pluralizes beyond one day', () => {
    expect(timeAgo(new Date(NOW - 5 * 86_400_000).toISOString())).toBe(
      '5d ago'
    );
    expect(timeAgo(new Date(NOW - 30 * 86_400_000).toISOString())).toBe(
      '30d ago'
    );
  });
});
