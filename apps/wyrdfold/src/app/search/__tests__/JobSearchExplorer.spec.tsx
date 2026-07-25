import React from 'react';
import '@testing-library/jest-dom';
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import JobSearchExplorer from '../JobSearchExplorer';
import type { JobSearchResult } from '../types';

// next/link needs the app-router context; stub to a plain anchor for the unit.
jest.mock('next/link', () => ({
  __esModule: true,
  default: ({
    href,
    children,
    ...rest
  }: {
    href: string;
    children: React.ReactNode;
  }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
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

// Funnel beacon (§10 PR6): assert the tick, never the wire (the emitter is
// fire-and-forget and must stay invisible to the rest of these tests).
const mockEmitSearchEvent = jest.fn();
jest.mock('../searchEvents', () => ({
  emitSearchEvent: (...args: unknown[]) => mockEmitSearchEvent(...args),
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

/** Resolve the same search page for every request — including the best-effort
 *  target-membership POST, whose `memberships` is simply absent here (unbound). */
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

/** The card is a single `role="button"` whose accessible name is its aria-label. */
function cardName(title = 'Frontend Engineer', company = 'Acme') {
  return `Open ${title} at ${company}`;
}

describe('JobSearchExplorer', () => {
  it('is framed as distinct from the matched Jobs view', () => {
    render(<JobSearchExplorer isAuthenticated />);
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

  it('searches the authed BFF route and renders result cards (no score)', async () => {
    mockSearch([result()]);
    render(<JobSearchExplorer isAuthenticated />);
    typeAndSearch('frontend engineer');

    // The whole result is a button (opens the detail) — not a title link.
    const card = await screen.findByRole('button', { name: cardName() });
    expect(within(card).getByText(/Acme · Remote/)).toBeInTheDocument();
    expect(within(card).getByText('$150k')).toBeInTheDocument();

    // Hit the logged-in search BFF route (auth-gated), and no numeric
    // match-score badge on a result (search is deliberately unscored).
    expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/jobs/search?q=frontend+engineer')
    );
    expect(screen.queryByText(/^\d{1,3}$/)).not.toBeInTheDocument();
  });

  it('opens the detail modal when a card is clicked (actions live there now)', async () => {
    mockSearch([result()]);
    render(<JobSearchExplorer isAuthenticated />);
    typeAndSearch('frontend');

    fireEvent.click(await screen.findByRole('button', { name: cardName() }));

    // The listing detail (a dialog) — the source link + the bind actions moved
    // off the card and into here (§11.2/§11.3).
    const dialog = await screen.findByRole('dialog');
    expect(
      within(dialog).getByRole('link', { name: /view original posting/i })
    ).toHaveAttribute('href', 'https://ext.example/1');
    expect(
      within(dialog).getByText(/unlock llm pipelines/i)
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole('button', {
        name: /create a target from this role/i,
      })
    ).toBeInTheDocument();
  });

  it('binds an unbound listing from the modal and unlocks match/tailor live (§11.3)', async () => {
    global.fetch = jest.fn().mockImplementation((url: string) => {
      if (url.includes('/api/targets/mine'))
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({
            targets: [{ target: { id: 't1', label: 'Frontend Roles' } }],
          }),
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
      if (url.includes('/api/jobs/target-membership'))
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({ memberships: {} }), // starts unbound
        });
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          query: 'q',
          count: 1,
          has_more: false,
          results: [result({ id: '1' })],
        }),
      });
    }) as unknown as typeof fetch;

    render(<JobSearchExplorer isAuthenticated />);
    typeAndSearch('frontend');

    fireEvent.click(await screen.findByRole('button', { name: cardName() }));
    const dialog = await screen.findByRole('dialog');
    // Unbound → no match link yet.
    expect(
      within(dialog).queryByRole('link', { name: /see how you match/i })
    ).not.toBeInTheDocument();

    // Bind it from the modal's picker.
    fireEvent.click(
      within(dialog).getByRole('button', { name: /add to target/i })
    );
    fireEvent.click(
      await screen.findByRole('menuitem', { name: 'Frontend Roles' })
    );

    // Live unlock: the match link now appears, pointing at /jobs/1.
    await waitFor(() =>
      expect(
        screen.getByRole('link', { name: /see how you match/i })
      ).toHaveAttribute('href', '/jobs/1')
    );
  });

  it('emits a card_open funnel tick (authed surface) when a card is opened', async () => {
    mockSearch([result()]);
    render(<JobSearchExplorer isAuthenticated />);
    typeAndSearch('frontend');
    fireEvent.click(await screen.findByRole('button', { name: cardName() }));
    expect(mockEmitSearchEvent).toHaveBeenCalledWith({
      event_type: 'card_open',
      surface: 'authed',
      job_posting_id: '1',
    });
  });

  it('opens the modal from the keyboard (Enter on the focused card)', async () => {
    mockSearch([result()]);
    render(<JobSearchExplorer isAuthenticated />);
    typeAndSearch('frontend');

    const card = await screen.findByRole('button', { name: cardName() });
    fireEvent.keyDown(card, { key: 'Enter' });
    expect(await screen.findByRole('dialog')).toBeInTheDocument();
  });

  it('fetches target-membership for the page and badges a bound listing (#467 §11)', async () => {
    global.fetch = jest.fn().mockImplementation((url: string) => {
      if (
        typeof url === 'string' &&
        url.includes('/api/jobs/target-membership')
      ) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({
            memberships: {
              '1': [{ target_id: 't1', label: 'Frontend Roles' }],
            },
          }),
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          query: 'q',
          count: 1,
          has_more: false,
          results: [result({ id: '1' })],
        }),
      });
    }) as unknown as typeof fetch;

    render(<JobSearchExplorer isAuthenticated />);
    typeAndSearch('frontend');

    // The card renders the "✓ In <target>" pipeline-state badge once membership
    // resolves — proving the POST fired and fed the map.
    expect(await screen.findByText(/Frontend Roles/)).toBeInTheDocument();
    const memCall = (global.fetch as jest.Mock).mock.calls.find(
      c =>
        typeof c[0] === 'string' && c[0].includes('/api/jobs/target-membership')
    );
    expect(memCall?.[1]?.method).toBe('POST');
    expect(memCall?.[1]?.body).toContain('"1"'); // job_posting_ids carries the id
  });

  it('renders the snippet preview on a result card (#467 §11)', async () => {
    mockSearch([
      result({ snippet: 'Build fast, accessible UIs for millions.' }),
    ]);
    render(<JobSearchExplorer isAuthenticated />);
    typeAndSearch('frontend');
    expect(
      await screen.findByText('Build fast, accessible UIs for millions.')
    ).toBeInTheDocument();
  });

  it('paginates via "Load more" and appends the next page (#467)', async () => {
    const page1 = [
      result({ id: '1', title: 'Frontend Engineer' }),
      result({ id: '2', title: 'Senior Frontend Engineer' }),
    ];
    const page2 = [result({ id: '3', title: 'Staff Frontend Engineer' })];
    global.fetch = jest.fn().mockImplementation((url: string) => {
      // Only the /search calls carry offset; the membership POST does not.
      const isSearch = url.includes('/api/jobs/search');
      const first = url.includes('offset=0');
      const results = isSearch ? (first ? page1 : page2) : [];
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          query: 'q',
          count: results.length,
          has_more: isSearch && first, // more after page 1, none after page 2
          results,
        }),
      });
    }) as unknown as typeof fetch;

    render(<JobSearchExplorer isAuthenticated />);
    typeAndSearch('frontend');

    // Page 1 + a "Load more" affordance.
    expect(
      await screen.findByRole('button', { name: cardName('Frontend Engineer') })
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /load more/i }));

    // Page 2 appended (page 1 rows still present), and "Load more" is gone.
    expect(
      await screen.findByRole('button', {
        name: cardName('Staff Frontend Engineer'),
      })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: cardName('Frontend Engineer') })
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(
        screen.queryByRole('button', { name: /load more/i })
      ).not.toBeInTheDocument()
    );
    // The second page paged from offset=2 (page 1 length). Not "last" call — the
    // best-effort membership POST fires after — so assert it was called at all.
    expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining('offset=2')
    );
  });

  it('shows an honest empty state when nothing matches', async () => {
    mockSearch([]);
    render(<JobSearchExplorer isAuthenticated />);
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
    render(<JobSearchExplorer isAuthenticated />);
    typeAndSearch('frontend');

    expect(await screen.findByText(/search exploded/i)).toBeInTheDocument();
  });

  // ---- URL-synced query + filters (#467) ---------------------------------

  it('restores the search straight from URL params on mount (back/bookmark)', async () => {
    mockSearch([result({ title: 'Frontend Engineer' })]);
    mockSearchParams = new URLSearchParams('q=engineer');
    render(<JobSearchExplorer isAuthenticated />);

    // No typing — the search runs off the URL alone.
    expect(
      await screen.findByRole('button', { name: cardName('Frontend Engineer') })
    ).toBeInTheDocument();
    expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/jobs/search?q=engineer')
    );
  });

  it('commits the query to the URL on submit', async () => {
    mockSearch([result()]);
    render(<JobSearchExplorer isAuthenticated />);
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
    render(<JobSearchExplorer isAuthenticated />);
    await screen.findByRole('button', { name: cardName('Frontend Engineer') });

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
    render(<JobSearchExplorer isAuthenticated />);
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
});

// ---- Logged-out (public) surface (#467 §10/§11) --------------------------
describe('JobSearchExplorer — logged out (public)', () => {
  /** Every fetch resolves the same one-result page — lets us assert WHICH URLs
   *  the logged-out explorer hits (and, crucially, which it does not). */
  function mockPublicPage() {
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        query: 'q',
        count: 1,
        has_more: false,
        results: [result()],
      }),
    }) as unknown as typeof fetch;
  }

  function fetchedUrls(): string[] {
    return (global.fetch as jest.Mock).mock.calls
      .map(c => c[0])
      .filter((u): u is string => typeof u === 'string');
  }

  it('emits a card_open tick with the PUBLIC surface when logged out', async () => {
    mockPublicPage();
    render(<JobSearchExplorer isAuthenticated={false} />);
    typeAndSearch('frontend');
    fireEvent.click(await screen.findByRole('button', { name: cardName() }));
    expect(mockEmitSearchEvent).toHaveBeenCalledWith({
      event_type: 'card_open',
      surface: 'public',
      job_posting_id: '1',
    });
  });

  it('uses the honest logged-out sub-copy (no "scored against your profile" steer)', () => {
    render(<JobSearchExplorer isAuthenticated={false} />);
    expect(screen.getByText(/no account needed/i)).toBeInTheDocument();
    expect(
      screen.queryByText(/scored against your profile/i)
    ).not.toBeInTheDocument();
    // No steer-to-Jobs link on the public surface.
    expect(
      screen.queryByRole('link', { name: 'Jobs' })
    ).not.toBeInTheDocument();
  });

  it('fetches the PUBLIC BFF route — never the authed search or membership', async () => {
    mockPublicPage();
    render(<JobSearchExplorer isAuthenticated={false} />);
    typeAndSearch('frontend engineer');

    await screen.findByRole('button', { name: cardName() });

    // Hits the public BFF route with the query...
    expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/public/search?q=frontend+engineer')
    );
    // ...and NEVER the authed search route or the per-user membership POST.
    const urls = fetchedUrls();
    expect(urls.some(u => u.includes('/api/jobs/search'))).toBe(false);
    expect(urls.some(u => u.includes('/api/jobs/target-membership'))).toBe(
      false
    );
  });

  it('renders a card with NO logged-in footer (pure click-target)', async () => {
    mockPublicPage();
    render(<JobSearchExplorer isAuthenticated={false} />);
    typeAndSearch('frontend');

    const card = await screen.findByRole('button', { name: cardName() });
    // The card still opens the detail (below), but carries no add/badge footer.
    expect(within(card).queryByText(/add to target/i)).not.toBeInTheDocument();
    expect(within(card).queryByText(/^In /)).not.toBeInTheDocument();
  });

  it('opens a detail with the source link + the soft signup allusion, not the bind/LLM actions', async () => {
    mockPublicPage();
    render(<JobSearchExplorer isAuthenticated={false} />);
    typeAndSearch('frontend');

    fireEvent.click(await screen.findByRole('button', { name: cardName() }));
    const dialog = await screen.findByRole('dialog');

    // Browsing stays free: the source link is present.
    expect(
      within(dialog).getByRole('link', { name: /view original posting/i })
    ).toHaveAttribute('href', 'https://ext.example/1');
    // The one soft conversion moment → /login.
    expect(
      within(dialog).getByRole('link', { name: /sign up free/i })
    ).toHaveAttribute('href', '/login');
    // NONE of the logged-in bind / LLM actions leak onto the public detail.
    expect(
      within(dialog).queryByText(/unlock llm pipelines/i)
    ).not.toBeInTheDocument();
    expect(
      within(dialog).queryByRole('button', {
        name: /create a target from this role/i,
      })
    ).not.toBeInTheDocument();
    expect(
      within(dialog).queryByRole('button', { name: /add to target/i })
    ).not.toBeInTheDocument();
    expect(
      within(dialog).queryByRole('link', { name: /see how you match/i })
    ).not.toBeInTheDocument();
  });
});
