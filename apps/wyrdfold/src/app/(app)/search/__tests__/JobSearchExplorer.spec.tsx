import React from 'react';
import '@testing-library/jest-dom';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
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

const mockToast = jest.fn();
jest.mock('@/state/Toast/ToastProvider', () => ({
  useToast: () => ({ toast: mockToast }),
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

function mockSearch(results: JobSearchResult[], hasMore = false) {
  global.fetch = jest.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({
      query: 'q',
      count: results.length,
      has_more: hasMore,
      results,
    }),
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

  it('creates a target from a listing via the from-url flow (#467)', async () => {
    const created = {
      user_target: { id: 'ut1' },
      target: { id: 't1', label: 'Frontend Engineer' },
      was_matched: false,
    };
    global.fetch = jest.fn().mockImplementation((url: string) => {
      if (typeof url === 'string' && url.includes('/api/targets/from-url')) {
        return Promise.resolve({
          ok: true,
          status: 201,
          json: async () => created,
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          query: 'q',
          count: 1,
          has_more: false,
          results: [result({ absolute_url: 'https://ext.example/1' })],
        }),
      });
    }) as unknown as typeof fetch;

    render(<JobSearchExplorer />);
    typeAndSearch('frontend');

    const createBtn = await screen.findByRole('button', {
      name: /create target/i,
    });
    fireEvent.click(createBtn);

    // Reuses the existing from-url flow with the listing's URL.
    await waitFor(() =>
      expect(global.fetch).toHaveBeenCalledWith(
        '/api/targets/from-url',
        expect.objectContaining({ method: 'POST' })
      )
    );
    const fromUrlCall = (global.fetch as jest.Mock).mock.calls.find(
      c => typeof c[0] === 'string' && c[0].includes('/api/targets/from-url')
    );
    expect(fromUrlCall?.[1]?.body).toContain('https://ext.example/1'); // jd_url = the listing
    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: 'success' })
      )
    );
  });

  it('surfaces an error toast when create-target fails, e.g. no profile (#467)', async () => {
    const payload = { detail: 'no prose doc to derive from' };
    global.fetch = jest.fn().mockImplementation((url: string) => {
      if (typeof url === 'string' && url.includes('/api/targets/from-url')) {
        return Promise.resolve({
          ok: false,
          status: 422,
          json: async () => payload,
          clone: () => ({ json: async () => payload }), // extractApiError reads via clone()
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          query: 'q',
          count: 1,
          has_more: false,
          results: [result()],
        }),
      });
    }) as unknown as typeof fetch;

    render(<JobSearchExplorer />);
    typeAndSearch('frontend');
    fireEvent.click(
      await screen.findByRole('button', { name: /create target/i })
    );

    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: 'error' })
      )
    );
  });

  it('paginates via "Load more" and appends the next page (#467)', async () => {
    const page1 = [
      result({ id: '1', title: 'Frontend Engineer' }),
      result({ id: '2', title: 'Senior Frontend Engineer' }),
    ];
    const page2 = [result({ id: '3', title: 'Staff Frontend Engineer' })];
    global.fetch = jest.fn().mockImplementation((url: string) => {
      const first = url.includes('offset=0');
      const results = first ? page1 : page2;
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          query: 'q',
          count: results.length,
          has_more: first, // more after page 1, none after page 2
          results,
        }),
      });
    }) as unknown as typeof fetch;

    render(<JobSearchExplorer />);
    typeAndSearch('frontend');

    // Page 1 + a "Load more" affordance.
    expect(
      await screen.findByRole('link', { name: 'Frontend Engineer' })
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /load more/i }));

    // Page 2 appended (page 1 rows still present), and "Load more" is gone.
    expect(
      await screen.findByRole('link', { name: 'Staff Frontend Engineer' })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('link', { name: 'Frontend Engineer' })
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(
        screen.queryByRole('button', { name: /load more/i })
      ).not.toBeInTheDocument()
    );
    // The second request paged from offset=2 (page 1 length).
    expect(global.fetch).toHaveBeenLastCalledWith(
      expect.stringContaining('offset=2')
    );
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
