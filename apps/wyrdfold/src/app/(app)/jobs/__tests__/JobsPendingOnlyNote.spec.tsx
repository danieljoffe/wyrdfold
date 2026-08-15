import React from 'react';
import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';
import JobsPendingOnlyNote from '../JobsPendingOnlyNote';
import type { JobPosting } from '../types';

// The §A3 sweep state: an explicit "Score 85+" chip left only ungraded rows
// on screen — blank score badges, no explanation. The note renders exactly
// then, and never over graded results or the un-filtered list.

function posting(overrides: Partial<JobPosting>): JobPosting {
  return {
    id: 'p1',
    external_id: 'e1',
    source_id: 's1',
    title: 'Engineer',
    company_name: 'Acme',
    location: null,
    absolute_url: null,
    score: 90,
    score_breakdown: null,
    scoring_status: 'stage1',
    status: 'new',
    salary_text: null,
    source_posted_at: null,
    cataloged_at: '2026-08-01T00:00:00Z',
    ...overrides,
  };
}

const NOTE = /still waiting for its full match grade/i;

describe('JobsPendingOnlyNote', () => {
  it('explains a pending-only result set under an explicit score filter', () => {
    render(
      <JobsPendingOnlyNote
        postings={[
          posting({ id: 'a', pending: true }),
          posting({ id: 'b', pending: true }),
        ]}
        loading={false}
        minScore='85'
      />
    );
    expect(screen.getByRole('note')).toHaveTextContent(NOTE);
  });

  it('stays silent when any row is graded', () => {
    render(
      <JobsPendingOnlyNote
        postings={[
          posting({ id: 'a', pending: true }),
          posting({ id: 'b', pending: false }),
        ]}
        loading={false}
        minScore='85'
      />
    );
    expect(screen.queryByText(NOTE)).not.toBeInTheDocument();
  });

  it('stays silent without an explicit score filter', () => {
    render(
      <JobsPendingOnlyNote
        postings={[posting({ id: 'a', pending: true })]}
        loading={false}
        minScore=''
      />
    );
    expect(screen.queryByText(NOTE)).not.toBeInTheDocument();
  });

  it('stays silent while loading and on empty results', () => {
    const { rerender } = render(
      <JobsPendingOnlyNote
        postings={[posting({ id: 'a', pending: true })]}
        loading={true}
        minScore='85'
      />
    );
    expect(screen.queryByText(NOTE)).not.toBeInTheDocument();
    rerender(
      <JobsPendingOnlyNote postings={[]} loading={false} minScore='85' />
    );
    expect(screen.queryByText(NOTE)).not.toBeInTheDocument();
  });

  it('treats a malformed or zero filter as no filter', () => {
    render(
      <JobsPendingOnlyNote
        postings={[posting({ id: 'a', pending: true })]}
        loading={false}
        minScore='0'
      />
    );
    expect(screen.queryByText(NOTE)).not.toBeInTheDocument();
  });
});
