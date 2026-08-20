import { formatJobSalary, formatSalaryRange } from '../formatSalary';

/**
 * One formatter for every salary display (#606) — the 2026-08-05 drive
 * counted seven raw formats coexisting on /jobs and /search.
 */
describe('formatSalaryRange', () => {
  it('compacts yearly ranges to k with at most one decimal', () => {
    expect(
      formatSalaryRange({
        min: 118600,
        max: 195680,
        currency: 'USD',
        period: 'yearly',
      })
    ).toBe('$118.6k–$195.7k');
    expect(
      formatSalaryRange({
        min: 191000,
        max: 253000,
        currency: 'USD',
        period: 'yearly',
      })
    ).toBe('$191k–$253k');
  });

  it('renders hourly figures plainly with /hr', () => {
    expect(
      formatSalaryRange({ min: 45, max: 60, currency: 'USD', period: 'hourly' })
    ).toBe('$45–$60/hr');
  });

  it('handles open-ended and single-point ranges', () => {
    expect(
      formatSalaryRange({
        min: 150000,
        max: null,
        currency: 'USD',
        period: 'yearly',
      })
    ).toBe('$150k+');
    expect(
      formatSalaryRange({
        min: null,
        max: 90000,
        currency: 'USD',
        period: 'yearly',
      })
    ).toBe('Up to $90k');
    expect(
      formatSalaryRange({
        min: 120000,
        max: 120000,
        currency: 'USD',
        period: 'yearly',
      })
    ).toBe('$120k');
  });

  it('prefixes non-USD currency codes and treats unknown period as yearly', () => {
    expect(
      formatSalaryRange({
        min: 100000,
        max: 130000,
        currency: 'CAD',
        period: null,
      })
    ).toBe('CAD 100k–130k');
  });

  it('renders symbol currencies with the symbol on both bounds', () => {
    // The sweep found "EUR 54k–EUR 75k" on prod (§D4) — known symbols now
    // render as symbols; codes render once, before the range.
    expect(
      formatSalaryRange({
        min: 54000,
        max: 75000,
        currency: 'EUR',
        period: null,
      })
    ).toBe('€54k–€75k');
    expect(
      formatSalaryRange({
        min: 60000,
        max: 80000,
        currency: 'GBP',
        period: null,
      })
    ).toBe('£60k–£80k');
  });

  it('returns null with no bounds', () => {
    expect(
      formatSalaryRange({ min: null, max: null, currency: null, period: null })
    ).toBeNull();
  });
});

describe('formatJobSalary', () => {
  it('prefers structured fields over the raw board text', () => {
    expect(
      formatJobSalary({
        salary_min: 118600,
        salary_max: 195680,
        salary_currency: 'USD',
        salary_period: 'yearly',
        salary_text: '$118,600.00 - $195,680.00',
      })
    ).toBe('$118.6k–$195.7k');
  });

  it('falls back to salary_text when parsing produced nothing', () => {
    expect(formatJobSalary({ salary_text: 'Competitive (see posting)' })).toBe(
      'Competitive (see posting)'
    );
    expect(formatJobSalary({})).toBeNull();
  });
});
