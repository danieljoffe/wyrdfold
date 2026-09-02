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

// The BFF is a thin forwarder to wyrdfold-api `GET /public/search`. Mock global
// fetch so each test controls / asserts the upstream call. This route carries
// NO user credential — it is the logged-out surface.
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
  query: string,
  headers: Record<string, string> = { 'x-real-ip': '10.0.0.1' }
): NextRequest {
  return new NextRequest(`http://localhost/api/public/search?${query}`, {
    method: 'GET',
    headers,
  });
}

/** The single upstream call fetch received: `[url, init]`. */
function lastCall(): [string, RequestInit] {
  return mockFetch.mock.calls[0] as [string, RequestInit];
}

/** URL parsed for its querystring, so param assertions don't depend on order. */
function forwardedParams(): URLSearchParams {
  const [url] = lastCall();
  return new URL(url).searchParams;
}

beforeEach(() => {
  jest.clearAllMocks();
  process.env['WYRDFOLD_API_URL'] = API_URL;
  // Each test controls the BFF secret explicitly; start clean so it can't bleed.
  delete process.env['WYRDFOLD_BFF_SECRET'];
  global.fetch = mockFetch as unknown as typeof fetch;
  mockFetch.mockResolvedValue(
    upstreamResponse(200, {
      query: 'frontend',
      count: 0,
      has_more: false,
      results: [],
    })
  );
});

describe('GET /api/public/search (public BFF forwarder)', () => {
  it('forwards the search params to the API /public/search and returns its body', async () => {
    const results = [{ id: '1', title: 'Frontend Engineer' }];
    mockFetch.mockResolvedValueOnce(
      upstreamResponse(200, {
        query: 'frontend',
        count: 1,
        has_more: true,
        results,
      })
    );

    const res = await GET(
      makeRequest(
        'q=frontend&page_size=20&offset=0&location=Remote&posted_within_days=7' +
          '&salary_floor=150000'
      )
    );

    expect(res.status).toBe(200);
    await expect(res.json()).resolves.toMatchObject({
      count: 1,
      has_more: true,
      results,
    });

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url, init] = lastCall();
    expect(url.startsWith(`${API_URL}/public/search?`)).toBe(true);
    expect(init.method).toBe('GET');

    const params = forwardedParams();
    expect(params.get('q')).toBe('frontend');
    expect(params.get('page_size')).toBe('20');
    expect(params.get('offset')).toBe('0');
    expect(params.get('location')).toBe('Remote');
    expect(params.get('posted_within_days')).toBe('7');
    expect(params.get('salary_floor')).toBe('150000');
  });

  it('trims q and only forwards the known search params (drops unknown args)', async () => {
    await GET(makeRequest('q=%20%20frontend%20%20&evil=1&admin=true'));
    const params = forwardedParams();
    expect(params.get('q')).toBe('frontend');
    // Smuggled params never reach the upstream.
    expect(params.has('evil')).toBe(false);
    expect(params.has('admin')).toBe(false);
  });

  it('sends NO Authorization/Bearer header (this is the logged-out surface)', async () => {
    await GET(makeRequest('q=frontend'));
    const [, init] = lastCall();
    const headers = new Headers(init.headers);
    expect(headers.get('authorization')).toBeNull();
  });

  it('injects the BFF shared secret when WYRDFOLD_BFF_SECRET is set', async () => {
    process.env['WYRDFOLD_BFF_SECRET'] = 'sekret';
    await GET(makeRequest('q=frontend'));
    const [, init] = lastCall();
    expect(new Headers(init.headers).get('x-wyrdfold-bff')).toBe('sekret');
  });

  it('omits the BFF secret header when WYRDFOLD_BFF_SECRET is unset', async () => {
    await GET(makeRequest('q=frontend'));
    const [, init] = lastCall();
    expect(new Headers(init.headers).get('x-wyrdfold-bff')).toBeNull();
  });

  it('forwards the Vercel-trusted x-real-ip so the backend keys its per-IP limit', async () => {
    await GET(makeRequest('q=frontend', { 'x-real-ip': '203.0.113.9' }));
    const [, init] = lastCall();
    expect(new Headers(init.headers).get('x-forwarded-for')).toBe('203.0.113.9');
  });

  it('does NOT trust a client-supplied x-forwarded-for (spoof defeat)', async () => {
    // Attacker prepends a fake IP and omits x-real-ip — the route relays no IP.
    await GET(
      makeRequest('q=frontend', { 'x-forwarded-for': '1.2.3.4, 5.6.7.8' })
    );
    const [, init] = lastCall();
    expect(new Headers(init.headers).get('x-forwarded-for')).toBeNull();
  });

  it('forwards a missing q as blank — browse mode (#834)', async () => {
    const res = await GET(makeRequest('page_size=20'));
    expect(res.status).toBe(200);
    expect(forwardedParams().get('q')).toBe('');
  });

  it('trims a whitespace-only q to blank and still forwards (#834)', async () => {
    const res = await GET(makeRequest('q=%20%20'));
    expect(res.status).toBe(200);
    expect(forwardedParams().get('q')).toBe('');
  });

  it('passes a backend rate-limit (429) through with Retry-After', async () => {
    mockFetch.mockResolvedValueOnce(
      upstreamResponse(
        429,
        { detail: 'Too many requests' },
        { 'retry-after': '42' }
      )
    );
    const res = await GET(makeRequest('q=frontend'));
    expect(res.status).toBe(429);
    expect(res.headers.get('Retry-After')).toBe('42');
  });

  it('passes a backend validation rejection (422) through', async () => {
    mockFetch.mockResolvedValueOnce(
      upstreamResponse(422, { detail: 'q too long' })
    );
    const res = await GET(makeRequest('q=frontend'));
    expect(res.status).toBe(422);
  });

  it('returns a generic 502 when the backend is unreachable, without leaking detail', async () => {
    mockFetch.mockRejectedValueOnce(new Error('ECONNREFUSED'));
    const res = await GET(makeRequest('q=frontend'));
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
    const res = await GET(makeRequest('q=frontend'));
    expect(res.status).toBe(502);
  });

  it('returns 503 (generic) when WYRDFOLD_API_URL is unset', async () => {
    delete process.env['WYRDFOLD_API_URL'];
    const res = await GET(makeRequest('q=frontend'));
    expect(res.status).toBe(503);
    expect(mockFetch).not.toHaveBeenCalled();
  });
});
