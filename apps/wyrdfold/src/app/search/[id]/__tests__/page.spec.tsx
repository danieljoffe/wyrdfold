import React from 'react';
import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';

import type { JobSearchResult } from '../../types';

// next/link needs the app-router context; stub to a plain anchor.
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

// The page is a server component: params → fetchListing → notFound()/render.
// Mock its two reads (listing + optional user) and pin the branching.
const mockFetchListing = jest.fn();
jest.mock('../fetchListing', () => ({
  fetchListing: (id: string) => mockFetchListing(id),
}));

const mockGetOptionalUser = jest.fn();
jest.mock('@/lib/supabase/getUser', () => ({
  getOptionalUser: () => mockGetOptionalUser(),
}));

// `notFound()` throws in Next; the sentinel lets the spec assert the 404 path.
const NOT_FOUND = new Error('NEXT_NOT_FOUND');
jest.mock('next/navigation', () => ({
  notFound: () => {
    throw NOT_FOUND;
  },
}));

const mockToast = jest.fn();
jest.mock('@/state/Toast/ToastProvider', () => ({
  useToast: () => ({ toast: mockToast }),
}));

jest.mock('../../searchEvents', () => ({
  emitSearchEvent: jest.fn(),
}));

import ListingPage, { generateMetadata } from '../page';

const ORIGINAL_FETCH = global.fetch;

const LISTING: JobSearchResult = {
  id: '1',
  title: 'Frontend Engineer',
  company_name: 'Acme',
  location: 'Remote',
  city: null,
  state: null,
  country: null,
  location_remote: null,
  salary_text: '$150k',
  absolute_url: 'https://ext.example/1',
  source_posted_at: null,
  cataloged_at: null,
  snippet: 'Build fast UIs.',
};

function pageProps(id = '1') {
  return { params: Promise.resolve({ id }) };
}

beforeEach(() => {
  jest.clearAllMocks();
  mockGetOptionalUser.mockResolvedValue(null);
  mockFetchListing.mockResolvedValue(LISTING);
  // The client body's best-effort reads (membership when authed) — benign empty.
  global.fetch = jest.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({ memberships: {} }),
  }) as unknown as typeof fetch;
});
afterAll(() => {
  global.fetch = ORIGINAL_FETCH;
});

describe('/search/[id] hard-load page (#467 §11.2 fast-follow)', () => {
  it('renders the shared listing as a full page with the browse-more on-ramp', async () => {
    render(await ListingPage(pageProps()));

    // The same detail body the modal shows...
    expect(
      screen.getByRole('heading', { name: 'Frontend Engineer' })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('link', { name: /view original posting/i })
    ).toHaveAttribute('href', 'https://ext.example/1');
    expect(screen.getByText('Build fast UIs.')).toBeInTheDocument();
    // ...plus the page-only on-ramp into the grid.
    expect(
      screen.getByRole('link', { name: /browse more jobs/i })
    ).toHaveAttribute('href', '/search');

    expect(mockFetchListing).toHaveBeenCalledWith('1');
  });

  it('logged out → keeps the soft signup allusion on the full page too (§11.5)', async () => {
    render(await ListingPage(pageProps()));
    expect(
      screen.getByRole('link', { name: /get early access/i })
    ).toHaveAttribute('href', '/login');
    // Never the authed bind/LLM actions.
    expect(screen.queryByText(/unlock fit analysis/i)).not.toBeInTheDocument();
  });

  it('signed in → threads isAuthenticated: bind actions, no signup allusion', async () => {
    mockGetOptionalUser.mockResolvedValue({ id: 'u1' });
    render(await ListingPage(pageProps()));

    expect(
      screen.getByText(
        /add to a target to unlock fit analysis and resume tailoring/i
      )
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('link', { name: /get early access/i })
    ).not.toBeInTheDocument();
  });

  it('calls notFound() when the listing is missing or delisted (API 404)', async () => {
    mockFetchListing.mockResolvedValue(null);
    await expect(ListingPage(pageProps('gone'))).rejects.toThrow(
      'NEXT_NOT_FOUND'
    );
  });

  describe('generateMetadata', () => {
    it('titles the page from the role and keeps it out of the index (§10 defers SEO)', async () => {
      const meta = await generateMetadata(pageProps());
      expect(meta.title).toBe('Frontend Engineer at Acme');
      expect(meta.robots).toEqual({ index: false, follow: false });
    });

    it('falls back to a generic title (still noindex) when the read fails', async () => {
      mockFetchListing.mockRejectedValue(new Error('api down'));
      const meta = await generateMetadata(pageProps());
      expect(meta.title).toBe('Job listing');
      expect(meta.robots).toEqual({ index: false, follow: false });
    });
  });
});
