import { formatCompanyName } from '../formatCompanyName';

/** Display repair for board-mangled company names (#606). */
describe('formatCompanyName', () => {
  it('repairs observed prod mangles via overrides', () => {
    expect(formatCompanyName('Redhat')).toBe('Red Hat');
    expect(formatCompanyName('Geaerospace')).toBe('GE Aerospace');
    expect(formatCompanyName('Oclc')).toBe('OCLC');
    expect(formatCompanyName('Wgu')).toBe('WGU');
    expect(formatCompanyName('Jj')).toBe('JJ');
  });

  it('de-slugs hyphenated lowercase names', () => {
    expect(formatCompanyName('hinge-health')).toBe('Hinge Health');
    expect(formatCompanyName('rox-data-corp')).toBe('Rox Data Corp');
  });

  it('capitalizes bare lowercase tokens', () => {
    expect(formatCompanyName('beaconai')).toBe('Beaconai');
    expect(formatCompanyName('careerswift.ai')).toBe('Careerswift.ai');
  });

  it('leaves healthy names alone', () => {
    expect(formatCompanyName('Anduril Industries')).toBe('Anduril Industries');
    expect(formatCompanyName('DEPT®')).toBe('DEPT®');
    expect(formatCompanyName('iCapital')).toBe('iCapital');
    expect(formatCompanyName('Cambridge Mobile Telematics')).toBe(
      'Cambridge Mobile Telematics'
    );
  });
});
