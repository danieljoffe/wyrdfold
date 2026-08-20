import { parseTab } from '../TargetDetail';

/**
 * `?tab=` is the shareable, sticky part of the target-detail URL. Anything
 * unrecognised falls back to Scoring silently — which is fine for garbage, but
 * the Reference JDs tab's slug (`jds`) is much shorter than its label, so a
 * link typed or remembered as `reference-jds` landed on the wrong tab with no
 * indication that it had (resweep C3, carried over from the first sweep).
 */
describe('parseTab', () => {
  it.each(['scoring', 'preferences', 'jds', 'learning'])(
    'passes through the real slug %s',
    slug => {
      expect(parseTab(slug)).toBe(slug);
    }
  );

  it.each([
    ['reference-jds', 'jds'],
    ['reference_jds', 'jds'],
    ['referencejds', 'jds'],
    ['jd', 'jds'],
    ['prefs', 'preferences'],
  ])('routes the plausible guess %s to %s', (raw, expected) => {
    expect(parseTab(raw)).toBe(expected);
  });

  it('is case- and whitespace-insensitive', () => {
    expect(parseTab('  Reference-JDs ')).toBe('jds');
    expect(parseTab('JDS')).toBe('jds');
  });

  it('still falls back to scoring for genuine nonsense', () => {
    expect(parseTab('not-a-tab')).toBe('scoring');
    expect(parseTab('')).toBe('scoring');
    expect(parseTab(null)).toBe('scoring');
  });
});
