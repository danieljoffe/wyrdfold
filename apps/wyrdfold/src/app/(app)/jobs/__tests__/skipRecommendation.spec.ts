import { isSkipRecommendation } from '../types';

/**
 * Gates the pre-spend confirm on the tailor buttons. A false POSITIVE adds
 * friction to a good match; a false negative just restores today's behaviour.
 * So the predicate is deliberately conservative — an explicit leading verdict
 * only, never a "skip" buried mid-sentence.
 */
describe('isSkipRecommendation', () => {
  it('matches the real prod verdict shape', () => {
    expect(
      isSkipRecommendation(
        'Skip: this is a Senior UX Designer role requiring 6+ years of product design experience, Figma fluency, and a design portfolio.'
      )
    ).toBe(true);
  });

  it('accepts the other decline verbs and is case-insensitive', () => {
    for (const v of [
      'skip — not a fit',
      'PASS: seniority mismatch',
      'Avoid. The domain is wrong.',
      "Don't apply, the stack doesn't overlap",
      'Do not apply — wrong level',
    ]) {
      expect(isSkipRecommendation(v)).toBe(true);
    }
  });

  it('does NOT fire on a positive recommendation', () => {
    for (const v of [
      'Apply: strong overlap on React and accessibility work.',
      'Strong match — lead with the component library work.',
      'Worth applying despite the seniority gap.',
    ]) {
      expect(isSkipRecommendation(v)).toBe(false);
    }
  });

  // The word appearing mid-sentence is not a verdict.
  it('does NOT fire on "skip" used incidentally', () => {
    expect(
      isSkipRecommendation(
        'Apply. You could skip the cover letter here — the referral matters more.'
      )
    ).toBe(false);
    expect(
      isSkipRecommendation('Strong match; do not skip the portfolio section.')
    ).toBe(false);
  });

  it('is false for an absent or empty analysis', () => {
    expect(isSkipRecommendation(null)).toBe(false);
    expect(isSkipRecommendation(undefined)).toBe(false);
    expect(isSkipRecommendation('')).toBe(false);
    expect(isSkipRecommendation('   ')).toBe(false);
  });
});
