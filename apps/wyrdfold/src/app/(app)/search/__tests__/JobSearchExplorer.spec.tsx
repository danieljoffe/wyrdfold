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

// Reactive next/navigation mock: router.replace updates the mocked
// searchParams and forces subscribed components to re-render, so the URL-driven
// search round-trip (submit/filter → URL → effect → fetch) runs in the unit.
let mockSearchParams = new URLSearchParams();
const mockNavListeners = new Set<() => void>();
const mockReplace = jest.fn((url: string) => {
  const i = url.indexOf('?');
  mockSearchParams = new URLSearchParams(i >= 0 ? url.slice(i + 1) : '');
  mockNavListeners.forEach(l => l());
});
jest.mock('next/navigation', () => {
  const react = require('react');
  return {
    useRouter: () => ({ replace: mockReplace, push: jest.fn() }),
    usePathname: () => '/search',
    useSearchParams: () => {
      const [, force] = react.useReducer((x: number) => x + 1, 0);
      react.useEffect(() => {
        mockNavListeners.add(force);
        return () => {
          mockNavListeners.delete(force);
        };
      }, []);
      return mockSearchParams;
    },
  };
});

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
    snippet: null,
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

beforeEach(() => {
  jest.clearAllMocks();
  mockSearchParams = new URLSearchParams();
  mockNavListeners.clear();
});
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
      expect.stringContaining('/api/jobs/search?q=frontend+engineer')
    );
    expect(screen.queryByText(/^\d{1,3}$/)).not.toBeInTheDocument();
  });

  it('renders the snippet preview on a result card (#467 §11)', async () => {
    mockSearch([
      result({ snippet: 'Build fast, accessible UIs for millions.' }),
    ]);
    render(<JobSearchExplorer />);
    typeAndSearch('frontend');
    expect(
      await screen.findByText('Build fast, accessible UIs for millions.')
    ).toBeInTheDocument();
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

  // ---- URL-synced query + filters (#467) ---------------------------------

  it('restores the search straight from URL params on mount (back/bookmark)', async () => {
    mockSearch([result({ title: 'Frontend Engineer' })]);
    mockSearchParams = new URLSearchParams('q=engineer');
    render(<JobSearchExplorer />);

    // No typing — the search runs off the URL alone.
    expect(
      await screen.findByRole('link', { name: 'Frontend Engineer' })
    ).toBeInTheDocument();
    expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/jobs/search?q=engineer')
    );
  });

  it('commits the query to the URL on submit', async () => {
    mockSearch([result()]);
    render(<JobSearchExplorer />);
    typeAndSearch('backend developer');
    await waitFor(() =>
      expect(mockReplace).toHaveBeenCalledWith(
        expect.stringContaining('q=backend+developer'),
        expect.anything()
      )
    );
  });

  it('applies the recency filter to the URL and the request', async () => {
    mockSearch([result()]);
    mockSearchParams = new URLSearchParams('q=engineer');
    render(<JobSearchExplorer />);
    await screen.findByRole('link', { name: 'Frontend Engineer' });

    fireEvent.change(screen.getByLabelText(/filter by date posted/i), {
      target: { value: '30' },
    });

    await waitFor(() =>
      expect(mockReplace).toHaveBeenCalledWith(
        expect.stringContaining('posted_within=30'),
        expect.anything()
      )
    );
    await waitFor(() =>
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining('posted_within_days=30')
      )
    );
  });

  it('applies the location filter to the request', async () => {
    mockSearch([result()]);
    render(<JobSearchExplorer />);
    fireEvent.change(
      screen.getByLabelText(/search jobs by title or keyword/i),
      { target: { value: 'engineer' } }
    );
    fireEvent.change(screen.getByLabelText(/filter by location/i), {
      target: { value: 'Remote' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^search$/i }));

    await waitFor(() =>
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining('location=Remote')
      )
    );
  });

  // ---- "Add to target" picker (#467 power-action) ------------------------

  function mockSearchPlus(
    handlers: (url: string) => Promise<unknown> | null
  ): void {
    global.fetch = jest.fn().mockImplementation((url: string) => {
      const handled = typeof url === 'string' ? handlers(url) : null;
      if (handled) return handled;
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          query: 'q',
          count: 1,
          has_more: false,
          results: [result()], // one row → one "Add to target" button
        }),
      });
    }) as unknown as typeof fetch;
  }

  const minePayload = {
    targets: [{ target: { id: 't1', label: 'Frontend Engineer' } }],
  };

  it('adds an existing listing to a chosen target via the picker (#467)', async () => {
    mockSearchPlus(url => {
      if (url.includes('/api/targets/mine'))
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => minePayload,
        });
      if (url.includes('/add-to-target'))
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({
            success: true,
            job_posting_id: '1',
            target_id: 't1',
            score: 70,
          }),
        });
      return null;
    });

    render(<JobSearchExplorer />);
    typeAndSearch('frontend');

    // Opening the menu lazily loads the user's targets.
    fireEvent.click(
      await screen.findByRole('button', { name: /add to target/i })
    );
    // A menuitem (distinct from the result's link) for the target.
    const option = await screen.findByRole('menuitem', {
      name: 'Frontend Engineer',
    });
    fireEvent.click(option);

    // POSTs the EXISTING job id to the add-to-target route with the target_id.
    await waitFor(() =>
      expect(global.fetch).toHaveBeenCalledWith(
        '/api/jobs/1/add-to-target',
        expect.objectContaining({ method: 'POST' })
      )
    );
    const addCall = (global.fetch as jest.Mock).mock.calls.find(
      c => typeof c[0] === 'string' && c[0].includes('/add-to-target')
    );
    expect(addCall?.[1]?.body).toContain('t1'); // target_id in the body
    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: 'success' })
      )
    );
  });

  it('surfaces an error toast when add-to-target fails (#467)', async () => {
    const payload = { detail: 'Target not found for user' };
    mockSearchPlus(url => {
      if (url.includes('/api/targets/mine'))
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => minePayload,
        });
      if (url.includes('/add-to-target'))
        return Promise.resolve({
          ok: false,
          status: 404,
          json: async () => payload,
          clone: () => ({ json: async () => payload }),
        });
      return null;
    });

    render(<JobSearchExplorer />);
    typeAndSearch('frontend');
    fireEvent.click(
      await screen.findByRole('button', { name: /add to target/i })
    );
    fireEvent.click(
      await screen.findByRole('menuitem', { name: 'Frontend Engineer' })
    );

    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: 'error' })
      )
    );
  });

  it('shows an empty picker state when the user has no targets (#467)', async () => {
    mockSearchPlus(url => {
      if (url.includes('/api/targets/mine'))
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({ targets: [] }),
        });
      return null;
    });

    render(<JobSearchExplorer />);
    typeAndSearch('frontend');
    fireEvent.click(
      await screen.findByRole('button', { name: /add to target/i })
    );

    expect(await screen.findByText(/no targets yet/i)).toBeInTheDocument();
  });
});
