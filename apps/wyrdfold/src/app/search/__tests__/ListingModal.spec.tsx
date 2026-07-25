import React from 'react';
import '@testing-library/jest-dom';
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import ListingModal from '../ListingModal';
import type { JobSearchResult } from '../types';

// next/link needs the app-router context; stub to a plain anchor (the detail
// body's signup allusion + the LinkButton match/tailor CTAs render through it).
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

// The intercepted route closes by unwinding history — router.back().
const mockBack = jest.fn();
jest.mock('next/navigation', () => ({
  useRouter: () => ({ back: mockBack, push: jest.fn(), replace: jest.fn() }),
}));

const mockToast = jest.fn();
jest.mock('@/state/Toast/ToastProvider', () => ({
  useToast: () => ({ toast: mockToast }),
}));

// Funnel beacon: assert nothing here — the card_open tick fires on the CARD
// click (explorer), not on the modal mounting from a shared link.
jest.mock('../searchEvents', () => ({
  emitSearchEvent: jest.fn(),
}));

const ORIGINAL_FETCH = global.fetch;

const LISTING: JobSearchResult = {
  id: '1',
  title: 'Frontend Engineer',
  company_name: 'Acme',
  location: 'Remote',
  department: null,
  salary_text: '$150k',
  absolute_url: 'https://ext.example/1',
  first_seen_at: null,
  created_at: null,
  snippet: 'Build fast UIs.',
};

/** Routes every fetch by URL; unrouted URLs resolve an empty OK JSON. */
function mockFetchRoutes(
  routes: Record<string, { ok?: boolean; status?: number; body?: unknown }>
): jest.Mock {
  const fn = jest.fn().mockImplementation((url: string) => {
    const hit = Object.entries(routes).find(([frag]) => url.includes(frag));
    const { ok = true, status = 200, body = {} } = hit?.[1] ?? {};
    return Promise.resolve({ ok, status, json: async () => body });
  });
  global.fetch = fn as unknown as typeof fetch;
  return fn;
}

function fetchedUrls(fn: jest.Mock): string[] {
  return fn.mock.calls
    .map(c => c[0])
    .filter((u): u is string => typeof u === 'string');
}

beforeEach(() => {
  jest.clearAllMocks();
});
afterAll(() => {
  global.fetch = ORIGINAL_FETCH;
});

describe('ListingModal (intercepted /search/[id] — #467 §11.2 fast-follow)', () => {
  it('shows the modal shell with a spinner while the listing loads', () => {
    // A fetch that never settles pins the loading state.
    global.fetch = jest.fn(
      () => new Promise(() => undefined)
    ) as unknown as typeof fetch;
    render(<ListingModal id='1' isAuthenticated={false} />);

    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByRole('status')).toBeInTheDocument();
    expect(within(dialog).getByText(/loading listing/i)).toBeInTheDocument();
  });

  it('fetches the listing from the public BFF route and renders the detail modal', async () => {
    const fn = mockFetchRoutes({ '/api/public/listings/1': { body: LISTING } });
    render(<ListingModal id='1' isAuthenticated={false} />);

    // NB: query via `screen` — the loading shell and the loaded JobDetailModal
    // are DIFFERENT Modal instances, so a dialog node captured while loading
    // detaches when the listing lands.
    expect(
      await screen.findByRole('heading', { name: 'Frontend Engineer' })
    ).toBeInTheDocument();
    const dialog = screen.getByRole('dialog');
    expect(
      within(dialog).getByRole('link', { name: /view original posting/i })
    ).toHaveAttribute('href', 'https://ext.example/1');
    expect(within(dialog).getByText('Build fast UIs.')).toBeInTheDocument();
    expect(
      fetchedUrls(fn).some(u => u.includes('/api/public/listings/1'))
    ).toBe(true);
  });

  it('renders the soft signup allusion (public detail) when logged out — and never fetches membership', async () => {
    const fn = mockFetchRoutes({ '/api/public/listings/1': { body: LISTING } });
    render(<ListingModal id='1' isAuthenticated={false} />);

    expect(
      await screen.findByRole('link', { name: /sign up free/i })
    ).toHaveAttribute('href', '/login');
    // Membership is a per-user concept — the logged-out modal must not call it.
    expect(
      fetchedUrls(fn).some(u => u.includes('/api/jobs/target-membership'))
    ).toBe(false);
  });

  it('shows the "listing unavailable" state on a 404, and Close unwinds history', async () => {
    mockFetchRoutes({
      '/api/public/listings/gone': {
        ok: false,
        status: 404,
        body: { error: 'Listing not found' },
      },
    });
    render(<ListingModal id='gone' isAuthenticated={false} />);

    expect(
      await screen.findByText(/this listing is unavailable/i)
    ).toBeInTheDocument();
    const dialog = screen.getByRole('dialog');

    fireEvent.click(within(dialog).getByRole('button', { name: /^close$/i }));
    expect(mockBack).toHaveBeenCalledTimes(1);
  });

  it('shows the same unavailable state when the fetch itself fails', async () => {
    global.fetch = jest
      .fn()
      .mockRejectedValue(new Error('network down')) as unknown as typeof fetch;
    render(<ListingModal id='1' isAuthenticated={false} />);

    expect(
      await screen.findByText(/this listing is unavailable/i)
    ).toBeInTheDocument();
  });

  it('closes the loaded detail via the dialog ✕ by unwinding history (router.back)', async () => {
    mockFetchRoutes({ '/api/public/listings/1': { body: LISTING } });
    render(<ListingModal id='1' isAuthenticated={false} />);

    await screen.findByRole('heading', { name: 'Frontend Engineer' });
    fireEvent.click(screen.getByRole('button', { name: /close dialog/i }));
    expect(mockBack).toHaveBeenCalledTimes(1);
  });

  it('authed: reads membership for THIS listing and renders the bound state', async () => {
    const fn = mockFetchRoutes({
      '/api/public/listings/1': { body: LISTING },
      '/api/jobs/target-membership': {
        body: {
          memberships: { '1': [{ target_id: 't9', label: 'Frontend Roles' }] },
        },
      },
    });
    render(<ListingModal id='1' isAuthenticated />);

    // Bound → the LLM actions are unlocked, scoped to the matched surface.
    expect(
      await screen.findByRole('link', { name: /see how you match/i })
    ).toHaveAttribute('href', '/jobs/1');
    expect(screen.getByText(/Frontend Roles/)).toBeInTheDocument();

    const memCall = fn.mock.calls.find(
      c =>
        typeof c[0] === 'string' && c[0].includes('/api/jobs/target-membership')
    );
    expect(memCall?.[1]?.method).toBe('POST');
    expect(memCall?.[1]?.body).toContain('"1"'); // job_posting_ids: [id]
  });

  it('authed: binds an unbound listing from the picker and unlocks match/tailor live (§11.3)', async () => {
    mockFetchRoutes({
      '/api/public/listings/1': { body: LISTING },
      '/api/jobs/target-membership': { body: { memberships: {} } }, // unbound
      '/api/targets/mine': {
        body: { targets: [{ target: { id: 't1', label: 'Frontend Roles' } }] },
      },
      '/add-to-target': {
        body: {
          success: true,
          job_posting_id: '1',
          target_id: 't1',
          score: 70,
        },
      },
    });
    render(<ListingModal id='1' isAuthenticated />);

    // Wait for the LOADED modal (see the instance-swap note above), then scope.
    await screen.findByRole('heading', { name: 'Frontend Engineer' });
    const dialog = screen.getByRole('dialog');
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
});
