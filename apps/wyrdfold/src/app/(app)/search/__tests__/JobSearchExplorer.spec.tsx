import React from 'react';
import '@testing-library/jest-dom';
import { fireEvent, render, screen } from '@testing-library/react';
import JobSearchExplorer from '../JobSearchExplorer';
import type { JobSearchResult } from '../types';

// next/link needs the app-router context; stub to a plain anchor for the unit.
jest.mock('next/link', () => ({
  __esModule: true,
  default: ({
    href,
    children,
  }: {
    href: string;
    children: React.ReactNode;
  }) => <a href={href}>{children}</a>,
}));

const ORIGINAL_FETCH = global.fetch;

function result(overrides: Partial<JobSearchResult> = {}): JobSearchResult {
  return {
    id: '1',
    title: 'Frontend Engineer',
    company_name: 'Acme',
    location: 'Remote',
    department: null,
    salary_text: '$150k',
    absolute_url: 'https://ext.example/1',
    first_seen_at: null,
    created_at: null,
    ...overrides,
  };
}

function mockSearch(results: JobSearchResult[]) {
  global.fetch = jest.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({ query: 'q', count: results.length, results }),
  }) as unknown as typeof fetch;
}

beforeEach(() => jest.clearAllMocks());
afterAll(() => {
  global.fetch = ORIGINAL_FETCH;
});

function typeAndSearch(term: string) {
  fireEvent.change(screen.getByLabelText(/search jobs by title or keyword/i), {
    target: { value: term },
  });
  fireEvent.click(screen.getByRole('button', { name: /search/i }));
}

describe('JobSearchExplorer', () => {
  it('is framed as distinct from the matched Jobs view', () => {
    render(<JobSearchExplorer />);
    expect(
      screen.getByRole('heading', { name: /search jobs/i })
    ).toBeInTheDocument();
    // Steers to Jobs for the AI match, and says these results aren't scored.
    expect(screen.getByRole('link', { name: 'Jobs' })).toHaveAttribute(
      'href',
      '/jobs'
    );
    expect(
      screen.getByText(/aren.t scored against your profile/i)
    ).toBeInTheDocument();
  });

  it('searches the authed BFF route and renders results linking to the source', async () => {
    mockSearch([result()]);
    render(<JobSearchExplorer />);
    typeAndSearch('frontend engineer');

    const link = await screen.findByRole('link', { name: 'Frontend Engineer' });
    expect(link).toHaveAttribute('href', 'https://ext.example/1');
    expect(link).toHaveAttribute('target', '_blank');
    expect(screen.getByText(/Acme · Remote/)).toBeInTheDocument();
    expect(screen.getByText('$150k')).toBeInTheDocument();

    // Hit the logged-in search BFF route (auth-gated). No numeric match-score
    // badge on a result row (the no-score guarantee is enforced by the API
    // payload + the JobSearchResult type; here just assert the row is a plain
    // preview).
    expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/jobs/search?q=frontend%20engineer')
    );
    expect(screen.queryByText(/^\d{1,3}$/)).not.toBeInTheDocument();
  });

  it('shows an honest empty state when nothing matches', async () => {
    mockSearch([]);
    render(<JobSearchExplorer />);
    typeAndSearch('nothingmatches');

    expect(await screen.findByText(/no roles match/i)).toBeInTheDocument();
    expect(screen.getByText(/expanding coverage/i)).toBeInTheDocument();
  });

  it('surfaces an error when the search fails', async () => {
    const payload = { detail: 'search exploded' };
    global.fetch = jest.fn().mockResolvedValue({
      ok: false,
      status: 500,
      json: async () => payload,
      // extractApiError reads the body via res.clone().json().
      clone: () => ({ json: async () => payload }),
    }) as unknown as typeof fetch;
    render(<JobSearchExplorer />);
    typeAndSearch('frontend');

    expect(await screen.findByText(/search exploded/i)).toBeInTheDocument();
  });
});
