import { fitScoreVariant } from './fitScore';

describe('fitScoreVariant', () => {
  it('is success (green) at or above 70', () => {
    expect(fitScoreVariant(70)).toBe('success');
    expect(fitScoreVariant(85)).toBe('success');
    expect(fitScoreVariant(100)).toBe('success');
  });

  it('is warning (amber) in [40, 70)', () => {
    expect(fitScoreVariant(40)).toBe('warning');
    expect(fitScoreVariant(55)).toBe('warning');
    expect(fitScoreVariant(69)).toBe('warning');
  });

  it('is error (red) below 40', () => {
    expect(fitScoreVariant(39)).toBe('error');
    expect(fitScoreVariant(1)).toBe('error');
    expect(fitScoreVariant(0)).toBe('error');
  });

  it('uses >= at each boundary (higher band wins)', () => {
    expect(fitScoreVariant(39.9)).toBe('error');
    expect(fitScoreVariant(40)).toBe('warning');
    expect(fitScoreVariant(69.9)).toBe('warning');
    expect(fitScoreVariant(70)).toBe('success');
  });
});
