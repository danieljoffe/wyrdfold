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

  // Sweep 2026-08-14 A5: feed catalog index leaked into the name and
  // surfaced on the tailored-resume header + export filename.
  describe('leading feed-index junk', () => {
    it('strips a zero-padded numeric prefix (the observed prod junk)', () => {
      expect(formatCompanyName('003 Humana Inc.')).toBe('Humana Inc.');
      expect(formatCompanyName('  003 Humana Inc.  ')).toBe('Humana Inc.');
      expect(formatCompanyName('01 Acme Corp')).toBe('Acme Corp');
    });

    it('does NOT strip genuinely numeric company names', () => {
      // No leading zero → a real name, not an index.
      expect(formatCompanyName('24 Hour Fitness')).toBe('24 Hour Fitness');
      // No space after the digits → the digits ARE the name.
      expect(formatCompanyName('3M')).toBe('3M');
      expect(formatCompanyName('7-Eleven')).toBe('7-Eleven');
      expect(formatCompanyName('H1')).toBe('H1');
      // Lowercase single token: digit rule doesn't apply, casing does.
      expect(formatCompanyName('37signals')).toBe('37signals');
    });

    it('keeps the original when stripping would leave nothing', () => {
      expect(formatCompanyName('003')).toBe('003');
      expect(formatCompanyName('0 ')).toBe('0');
    });
  });
});
