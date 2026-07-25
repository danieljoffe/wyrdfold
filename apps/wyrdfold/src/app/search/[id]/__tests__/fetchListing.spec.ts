/**
 * @jest-environment node
 */

// Server Components read `headers()` from next/headers — feed it a mutable bag
// so each test controls what the "request" carried.
let mockHeaderBag: Record<string, string> = {};
jest.mock('next/headers', () => ({
  headers: async () => new Headers(mockHeaderBag),
}));

import { fetchListing } from '../fetchListing';

const API_URL = 'http://api.test';
const LISTING_ID = '123e4567-e89b-42d3-a456-426614174000';

const mockFetch = jest.fn();

function upstream(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

function lastCall(): [string, RequestInit] {
  return mockFetch.mock.calls[0] as [string, RequestInit];
}

beforeEach(() => {
  jest.clearAllMocks();
  mockHeaderBag = { 'x-real-ip': '10.0.0.1' };
  process.env['WYRDFOLD_API_URL'] = API_URL;
  delete process.env['WYRDFOLD_BFF_SECRET'];
  global.fetch = mockFetch as unknown as typeof fetch;
  mockFetch.mockResolvedValue(
    upstream(200, { id: LISTING_ID, title: 'Frontend Engineer' })
  );
});

describe('fetchListing (server-side read for the hard-load /search/[id] page)', () => {
  it('fetches the API /public/listings/{id} directly and returns the listing', async () => {
    const listing = await fetchListing(LISTING_ID);
    expect(listing).toMatchObject({
      id: LISTING_ID,
      title: 'Frontend Engineer',
    });
    const [url, init] = lastCall();
    expect(url).toBe(`${API_URL}/public/listings/${LISTING_ID}`);
    // Public data — never a user credential on this read.
    expect(new Headers(init.headers).get('authorization')).toBeNull();
  });

  it('URL-encodes a hostile id so it stays one path segment', async () => {
    mockFetch.mockResolvedValueOnce(upstream(422, { detail: [] }));
    await fetchListing('../admin');
    const [url] = lastCall();
    expect(url).toBe(
      `${API_URL}/public/listings/${encodeURIComponent('../admin')}`
    );
  });

  it('injects the BFF shared secret when configured (same posture as the forwarders)', async () => {
    process.env['WYRDFOLD_BFF_SECRET'] = 'sekret';
    await fetchListing(LISTING_ID);
    const [, init] = lastCall();
    expect(new Headers(init.headers).get('x-wyrdfold-bff')).toBe('sekret');
  });

  it('forwards the Vercel-trusted x-real-ip so the per-IP limit keys on the visitor', async () => {
    mockHeaderBag = { 'x-real-ip': '203.0.113.9' };
    await fetchListing(LISTING_ID);
    const [, init] = lastCall();
    expect(new Headers(init.headers).get('x-forwarded-for')).toBe(
      '203.0.113.9'
    );
  });

  it('does NOT trust a client-supplied x-forwarded-for (spoof defeat)', async () => {
    mockHeaderBag = { 'x-forwarded-for': '1.2.3.4, 5.6.7.8' };
    await fetchListing(LISTING_ID);
    const [, init] = lastCall();
    expect(new Headers(init.headers).get('x-forwarded-for')).toBeNull();
  });

  it('returns null on 404 (missing or delisted) — the page maps it to notFound()', async () => {
    mockFetch.mockResolvedValueOnce(
      upstream(404, { detail: 'Listing not found' })
    );
    await expect(fetchListing(LISTING_ID)).resolves.toBeNull();
  });

  it('returns null on 422 (junk-shaped id) — equally "no such listing"', async () => {
    mockFetch.mockResolvedValueOnce(upstream(422, { detail: [] }));
    await expect(fetchListing('not-a-uuid')).resolves.toBeNull();
  });

  it('throws on other upstream failures — an outage must not masquerade as 404', async () => {
    mockFetch.mockResolvedValueOnce(upstream(503, { detail: 'down' }));
    await expect(fetchListing(LISTING_ID)).rejects.toThrow('503');
  });

  it('throws when WYRDFOLD_API_URL is unset (misconfiguration, loudly)', async () => {
    delete process.env['WYRDFOLD_API_URL'];
    await expect(fetchListing(LISTING_ID)).rejects.toThrow('WYRDFOLD_API_URL');
    expect(mockFetch).not.toHaveBeenCalled();
  });
});
