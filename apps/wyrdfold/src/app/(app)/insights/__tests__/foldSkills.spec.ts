import {
  cleanSkillLabel,
  foldMissingSkills,
  foldSkillFrequencies,
} from '../foldSkills';

/**
 * Display-side skill folding (#605). Prod evidence 2026-08-05: the Trends
 * view rendered "State management 2/3" and "State Management 1/3" as
 * separate rows, three near-identical monitoring rows, and grader
 * evidence clauses inside user-facing labels.
 */
describe('cleanSkillLabel', () => {
  it('strips an em-dash grader evidence clause', () => {
    expect(
      cleanSkillLabel(
        'Automated testing (Jest, Playwright, Cypress) — listed in skills with no evidence refs'
      )
    ).toBe('Automated testing (Jest, Playwright, Cypress)');
    expect(
      cleanSkillLabel(
        'Explicit state management library experience — no evidence in payload'
      )
    ).toBe('Explicit state management library experience');
  });

  it('collapses whitespace and leaves clean labels alone', () => {
    expect(cleanSkillLabel('  React   Server  Components ')).toBe(
      'React Server Components'
    );
    expect(cleanSkillLabel('Accessibility (WCAG)')).toBe(
      'Accessibility (WCAG)'
    );
  });
});

describe('foldSkillFrequencies', () => {
  it('merges case variants, summing counts, and sorts by total mentions', () => {
    const folded = foldSkillFrequencies([
      { skill: 'State management', matched_count: 2, missing_count: 1 },
      { skill: 'State Management', matched_count: 1, missing_count: 2 },
      { skill: 'TypeScript', matched_count: 16, missing_count: 0 },
    ]);

    expect(folded).toEqual([
      { skill: 'TypeScript', matched_count: 16, missing_count: 0 },
      { skill: 'State management', matched_count: 3, missing_count: 3 },
    ]);
  });

  it('merges rows that differ only by a grader clause', () => {
    const folded = foldSkillFrequencies([
      { skill: 'SQL', matched_count: 0, missing_count: 2 },
      {
        skill: 'SQL — no evidence in payload',
        matched_count: 0,
        missing_count: 1,
      },
    ]);
    expect(folded).toEqual([
      { skill: 'SQL', matched_count: 0, missing_count: 3 },
    ]);
  });
});

describe('foldMissingSkills', () => {
  it('merges duplicates with a weighted average score and keeps priority ordering', () => {
    const folded = foldMissingSkills([
      {
        skill: 'Monitoring / Analytics Tools (Datadog, New Relic, or similar)',
        missing_count: 1,
        avg_job_score: 83,
        priority_score: 10,
      },
      {
        skill:
          'Performance monitoring and analytics tools (Datadog, New Relic, or similar) — no evidence in payload',
        missing_count: 1,
        avg_job_score: 83,
        priority_score: 9,
      },
      {
        skill: 'monitoring / analytics tools (Datadog, New Relic, or similar)',
        missing_count: 3,
        avg_job_score: 90,
        priority_score: 12,
      },
    ]);

    // The two case-variants of the same label fold; the differently-worded
    // row stays separate (no fuzzy matching — display folding only).
    expect(folded).toHaveLength(2);
    const monitoring = folded[0];
    expect(monitoring?.skill).toBe(
      'Monitoring / Analytics Tools (Datadog, New Relic, or similar)'
    );
    expect(monitoring?.missing_count).toBe(4);
    expect(monitoring?.priority_score).toBe(12);
    expect(monitoring?.avg_job_score).toBeCloseTo((83 * 1 + 90 * 3) / 4);
  });

  it('keeps a null average when no variant carries a score', () => {
    const folded = foldMissingSkills([
      {
        skill: 'Kotlin',
        missing_count: 2,
        avg_job_score: null,
        priority_score: 5,
      },
      {
        skill: 'kotlin',
        missing_count: 1,
        avg_job_score: null,
        priority_score: 4,
      },
    ]);
    expect(folded).toEqual([
      {
        skill: 'Kotlin',
        missing_count: 3,
        avg_job_score: null,
        priority_score: 5,
      },
    ]);
  });
});
