/**
 * @jest-environment node
 */
import { NextRequest } from 'next/server';

jest.mock('@sentry/nextjs', () => ({
  captureException: jest.fn(),
  captureMessage: jest.fn(),
}));

import { GET } from './route';

const API_URL = 'http://api.test';
const LISTING_ID = '123e4567-e89b-42d3-a456-426614174000';

// The BFF is a thin forwarder to wyrdfold-api `GET /public/listings/{id}`. Mock
// global fetch so each test controls / asserts the upstream call. This route
// carries NO user credential — the shareable-listing surface is public data.
const mockFetch = jest.fn();

function upstreamResponse(
  status: number,
  body: unknown,
  headers: Record<string, string> = {}
): Response {
  const text = typeof body === 'string' ? body : JSON.stringify(body);
  return new Response(text, {
    status,
    headers: { 'content-type': 'application/json', ...headers },
  });
}

function makeRequest(
  headers: Record<string, string> = { 'x-real-ip': '10.0.0.1' }
): NextRequest {
  return new NextRequest(`http://localhost/api/public/listings/${LISTING_ID}`, {
    method: 'GET',
    headers,
  });
}

/** Invoke the handler the way Next does: request + a params Promise. */
function get(id: string, headers?: Record<string, string>) {
  return GET(makeRequest(headers), { params: Promise.resolve({ id }) });
}

/** The single upstream call fetch received: `[url, init]`. */
function lastCall(): [string, RequestInit] {
  return mockFetch.mock.calls[0] as [string, RequestInit];
}

beforeEach(() => {
  jest.clearAllMocks();
  process.env['WYRDFOLD_API_URL'] = API_URL;
  // Each test controls the BFF secret explicitly; start clean so it can't bleed.
  delete process.env['WYRDFOLD_BFF_SECRET'];
  global.fetch = mockFetch as unknown as typeof fetch;
  mockFetch.mockResolvedValue(
    upstreamResponse(200, {
      id: LISTING_ID,
      title: 'Frontend Engineer',
      company_name: 'Acme',
      snippet: 'Build fast UIs.',
    })
  );
});

describe('GET /api/public/listings/[id] (public BFF forwarder)', () => {
  it('forwards the id to the API /public/listings and returns its body', async () => {
    const res = await get(LISTING_ID);

    expect(res.status).toBe(200);
    await expect(res.json()).resolves.toMatchObject({
      id: LISTING_ID,
      title: 'Frontend Engineer',
      snippet: 'Build fast UIs.',
    });

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url, init] = lastCall();
    expect(url).toBe(`${API_URL}/public/listings/${LISTING_ID}`);
    expect(init.method).toBe('GET');
  });

  it('URL-encodes a hostile id so it stays one path segment upstream', async () => {
    mockFetch.mockResolvedValueOnce(upstreamResponse(422, { detail: [] }));
    await get('../admin?x=1');
    const [url] = lastCall();
    // No raw traversal/query characters reach the upstream path.
    expect(url).toBe(
      `${API_URL}/public/listings/${encodeURIComponent('../admin?x=1')}`
    );
  });

  it('sends NO Authorization/Bearer header (public data; membership is a separate authed call)', async () => {
    await get(LISTING_ID);
    const [, init] = lastCall();
    expect(new Headers(init.headers).get('authorization')).toBeNull();
  });

  it('injects the BFF shared secret when WYRDFOLD_BFF_SECRET is set', async () => {
    process.env['WYRDFOLD_BFF_SECRET'] = 'sekret';
    await get(LISTING_ID);
    const [, init] = lastCall();
    expect(new Headers(init.headers).get('x-wyrdfold-bff')).toBe('sekret');
  });

  it('omits the BFF secret header when WYRDFOLD_BFF_SECRET is unset', async () => {
    await get(LISTING_ID);
    const [, init] = lastCall();
    expect(new Headers(init.headers).get('x-wyrdfold-bff')).toBeNull();
  });

  it('forwards the Vercel-trusted x-real-ip so the backend keys its per-IP limit', async () => {
    await get(LISTING_ID, { 'x-real-ip': '203.0.113.9' });
    const [, init] = lastCall();
    expect(new Headers(init.headers).get('x-forwarded-for')).toBe('203.0.113.9');
  });

  it('does NOT trust a client-supplied x-forwarded-for (spoof defeat)', async () => {
    await get(LISTING_ID, { 'x-forwarded-for': '1.2.3.4, 5.6.7.8' });
    const [, init] = lastCall();
    expect(new Headers(init.headers).get('x-forwarded-for')).toBeNull();
  });

  it('passes a backend 404 through (missing OR delisted listing)', async () => {
    mockFetch.mockResolvedValueOnce(
      upstreamResponse(404, { detail: 'Listing not found' })
    );
    const res = await get(LISTING_ID);
    expect(res.status).toBe(404);
    await expect(res.json()).resolves.toEqual({ error: 'Listing not found' });
  });

  it('passes a backend validation rejection (422, junk id) through', async () => {
    mockFetch.mockResolvedValueOnce(
      upstreamResponse(422, { detail: [{ msg: 'uuid expected' }] })
    );
    const res = await get('not-a-uuid');
    expect(res.status).toBe(422);
    // The list-shaped FastAPI detail is not a string → generic body, no leak.
    const body = (await res.json()) as { error?: string };
    expect(body.error).toBe('Something went wrong. Please try again.');
  });

  it('passes a backend rate-limit (429) through with Retry-After', async () => {
    mockFetch.mockResolvedValueOnce(
      upstreamResponse(
        429,
        { detail: 'Too many requests' },
        { 'retry-after': '42' }
      )
    );
    const res = await get(LISTING_ID);
    expect(res.status).toBe(429);
    expect(res.headers.get('Retry-After')).toBe('42');
  });

  it('returns a generic 502 when the backend is unreachable, without leaking detail', async () => {
    mockFetch.mockRejectedValueOnce(new Error('ECONNREFUSED'));
    const res = await get(LISTING_ID);
    expect(res.status).toBe(502);
    const body = (await res.json()) as { error?: string };
    expect(body.error).toBeTruthy();
    expect(body.error).not.toContain('ECONNREFUSED');
  });

  it('returns 502 (not raw text) when the upstream 200 is non-JSON', async () => {
    mockFetch.mockResolvedValueOnce(
      new Response('<html>gateway</html>', {
        status: 200,
        headers: { 'content-type': 'text/html' },
      })
    );
    const res = await get(LISTING_ID);
    expect(res.status).toBe(502);
  });

  it('returns 503 (generic) when WYRDFOLD_API_URL is unset', async () => {
    delete process.env['WYRDFOLD_API_URL'];
    const res = await get(LISTING_ID);
    expect(res.status).toBe(503);
    expect(mockFetch).not.toHaveBeenCalled();
  });
});
