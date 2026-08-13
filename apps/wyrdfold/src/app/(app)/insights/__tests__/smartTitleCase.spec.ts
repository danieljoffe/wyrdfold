import { smartTitleCase } from '../smartTitleCase';

// The fixtures are the exact title_norm values observed on the prod Trends
// card during the 2026-08-12 UX sweep (docs/ux-sweep-2026-08-12.md §B1) —
// the mangles this module exists to fix.

describe('smartTitleCase', () => {
  it('uppercases known acronyms that CSS capitalize mangled', () => {
    expect(smartTitleCase('senior associate, it internal auditor')).toBe(
      'Senior Associate, IT Internal Auditor'
    );
    expect(smartTitleCase('gtm system analyst')).toBe('GTM System Analyst');
    expect(smartTitleCase('b2c customer experience associate i')).toBe(
      'B2C Customer Experience Associate I'
    );
    expect(smartTitleCase('it support engineer')).toBe('IT Support Engineer');
  });

  it('uppercases roman-numeral level suffixes', () => {
    expect(smartTitleCase('software engineer iii - ai services')).toBe(
      'Software Engineer III - AI Services'
    );
    expect(smartTitleCase('digital content associate principal')).toBe(
      'Digital Content Associate Principal'
    );
  });

  it('cases per part across &, / and - separators', () => {
    expect(
      smartTitleCase('manager / senior manager - cloud, data & ai (cd&ai)')
    ).toBe('Manager / Senior Manager - Cloud, Data & AI (CD&AI)');
    expect(smartTitleCase('sr c/c++ software developer')).toBe(
      'Sr C/C++ Software Developer'
    );
  });

  it('strips normalizer underscore artifacts', () => {
    expect(
      smartTitleCase('project engineer _field application engineering')
    ).toBe('Project Engineer Field Application Engineering');
  });

  it('keeps +, # and apostrophes inside a word', () => {
    expect(smartTitleCase("master's data steward c#")).toBe(
      "Master's Data Steward C#"
    );
  });

  it('applies mixed-case brand spellings', () => {
    expect(smartTitleCase('senior ios devops engineer')).toBe(
      'Senior iOS DevOps Engineer'
    );
  });

  it('leaves unknown parenthesized acronyms merely capitalized (documented limit)', () => {
    // "aht" is a company-internal acronym we can't know; the durable fix is
    // a title_display column on the store (plan §Phase 4).
    expect(smartTitleCase('principal ai software engineer (aht)')).toBe(
      'Principal AI Software Engineer (Aht)'
    );
  });

  it('never resolves prototype-named words through Object.prototype', () => {
    expect(smartTitleCase('constructor')).toBe('Constructor');
    expect(smartTitleCase('valueof toString')).toBe('Valueof Tostring');
  });

  it('is safe on degenerate input', () => {
    expect(smartTitleCase('')).toBe('');
    expect(smartTitleCase('   ')).toBe('');
    expect(smartTitleCase('___')).toBe('');
    expect(smartTitleCase('2027 intern software engineer')).toBe(
      '2027 Intern Software Engineer'
    );
  });
});
