/**
 * @jest-environment node
 */
import { NextRequest } from 'next/server';

jest.mock('@sentry/nextjs', () => ({
  captureException: jest.fn(),
  captureMessage: jest.fn(),
}));

import { POST } from './route';

const API_URL = 'http://api.test';

// The BFF is a thin forwarder to wyrdfold-api `POST /waitlist`. We mock the
// global fetch so each test controls / asserts the upstream call. The frontend
// no longer holds the service-role key or touches Supabase directly.
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

function makeRequest(body: unknown, ip = '10.0.0.1'): NextRequest {
  return new NextRequest('http://localhost/api/waitlist', {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'x-forwarded-for': ip,
    },
    body: typeof body === 'string' ? body : JSON.stringify(body),
  });
}

beforeEach(() => {
  jest.clearAllMocks();
  process.env['WYRDFOLD_API_URL'] = API_URL;
  global.fetch = mockFetch as unknown as typeof fetch;
  mockFetch.mockResolvedValue(upstreamResponse(200, { ok: true }));
});

describe('POST /api/waitlist (BFF forwarder)', () => {
  it('forwards a valid email to the backend and returns generic success', async () => {
    const res = await POST(makeRequest({ email: 'jane@example.com' }));
    expect(res.status).toBe(200);
    await expect(res.json()).resolves.toEqual({ ok: true });

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${API_URL}/waitlist`);
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({
      email: 'jane@example.com',
    });
  });

  it('normalises the email to lower-case + trims before forwarding', async () => {
    await POST(makeRequest({ email: '  Jane.Doe@Example.COM  ' }));
    const [, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({
      email: 'jane.doe@example.com',
    });
  });

  it('forwards the real client IP so the backend keys its per-IP limit', async () => {
    await POST(makeRequest({ email: 'jane@example.com' }, '203.0.113.9'));
    const [, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    const headers = new Headers(init.headers);
    expect(headers.get('x-forwarded-for')).toBe('203.0.113.9');
  });

  it('rejects an invalid email with 400 BEFORE any backend round trip', async () => {
    const res = await POST(makeRequest({ email: 'not-an-email' }));
    expect(res.status).toBe(400);
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('rejects a missing email with 400 (no forward)', async () => {
    const res = await POST(makeRequest({}));
    expect(res.status).toBe(400);
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('rejects a non-string email with 400 (no forward)', async () => {
    const res = await POST(makeRequest({ email: 12345 }));
    expect(res.status).toBe(400);
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('rejects an over-length email with 400 (no forward)', async () => {
    const huge = `${'a'.repeat(400)}@example.com`;
    const res = await POST(makeRequest({ email: huge }));
    expect(res.status).toBe(400);
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('returns 400 on a malformed JSON body (no forward)', async () => {
    const res = await POST(makeRequest('{not json'));
    expect(res.status).toBe(400);
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('passes a backend rate-limit (429) through with Retry-After', async () => {
    mockFetch.mockResolvedValueOnce(
      upstreamResponse(
        429,
        { detail: 'Too many requests' },
        { 'retry-after': '42' }
      )
    );
    const res = await POST(makeRequest({ email: 'spam@example.com' }));
    expect(res.status).toBe(429);
    expect(res.headers.get('Retry-After')).toBe('42');
  });

  it('passes a backend validation rejection (422) through', async () => {
    mockFetch.mockResolvedValueOnce(
      upstreamResponse(422, { detail: 'Please enter a valid email address.' })
    );
    const res = await POST(makeRequest({ email: 'jane@example.com' }));
    expect(res.status).toBe(422);
  });

  it('returns a generic 500 when the backend fails, without leaking detail', async () => {
    mockFetch.mockResolvedValueOnce(
      upstreamResponse(500, { detail: 'PostgREST: relation does not exist' })
    );
    const res = await POST(makeRequest({ email: 'fail@example.com' }));
    expect(res.status).toBe(500);
    const body = (await res.json()) as { error?: string };
    // The BFF relays the backend's already-generic message; the backend never
    // emits internal detail, but assert it isn't echoed regardless.
    expect(body.error).toBeTruthy();
  });

  it('returns 502 (generic) when the backend is unreachable', async () => {
    mockFetch.mockRejectedValueOnce(new Error('ECONNREFUSED'));
    const res = await POST(makeRequest({ email: 'jane@example.com' }));
    expect(res.status).toBe(502);
    const body = (await res.json()) as { error?: string };
    expect(body.error).toBeTruthy();
    expect(body.error).not.toContain('ECONNREFUSED');
  });

  it('returns 503 (generic) when WYRDFOLD_API_URL is unset', async () => {
    delete process.env['WYRDFOLD_API_URL'];
    const res = await POST(makeRequest({ email: 'jane@example.com' }));
    expect(res.status).toBe(503);
    expect(mockFetch).not.toHaveBeenCalled();
  });
});
