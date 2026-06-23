/**
 * @jest-environment node
 */
import { NextRequest } from 'next/server';

// Capture the upsert call so each test can assert on / control it.
const mockUpsert = jest.fn();
const mockFrom = jest.fn(() => ({ upsert: mockUpsert }));
const mockCreateServiceRoleClient = jest.fn(() => ({ from: mockFrom }));

jest.mock('@/lib/supabase/admin-client', () => ({
  createServiceRoleClient: () => mockCreateServiceRoleClient(),
}));

jest.mock('@sentry/nextjs', () => ({
  captureException: jest.fn(),
}));

import { POST } from './route';

// Each test gets a fresh IP so the module-level rate limiter (5 / 10min)
// doesn't bleed budget between cases. The rate-limit test uses its own IP and
// deliberately exhausts it.
let ipCounter = 0;
function freshIp(): string {
  ipCounter += 1;
  return `10.0.0.${ipCounter}`;
}

function makeRequest(body: unknown, ip = freshIp()): NextRequest {
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
  mockUpsert.mockResolvedValue({ error: null });
});

describe('POST /api/waitlist', () => {
  it('inserts a valid email and returns generic success', async () => {
    const res = await POST(makeRequest({ email: 'jane@example.com' }));
    expect(res.status).toBe(200);
    await expect(res.json()).resolves.toEqual({ ok: true });

    expect(mockFrom).toHaveBeenCalledWith('waitlist_signups');
    expect(mockUpsert).toHaveBeenCalledWith(
      { email: 'jane@example.com' },
      { onConflict: 'email', ignoreDuplicates: true }
    );
  });

  it('normalises the email to lower-case and trims whitespace', async () => {
    await POST(makeRequest({ email: '  Jane.Doe@Example.COM  ' }));
    expect(mockUpsert).toHaveBeenCalledWith(
      { email: 'jane.doe@example.com' },
      expect.anything()
    );
  });

  it('treats a duplicate email as success without revealing existence', async () => {
    // ignoreDuplicates → no error on conflict; the route must still 200.
    mockUpsert.mockResolvedValueOnce({ error: null });
    const res = await POST(makeRequest({ email: 'dup@example.com' }));
    expect(res.status).toBe(200);
    await expect(res.json()).resolves.toEqual({ ok: true });
  });

  it('rejects an invalid email with 400 and never touches the DB', async () => {
    const res = await POST(makeRequest({ email: 'not-an-email' }));
    expect(res.status).toBe(400);
    expect(mockCreateServiceRoleClient).not.toHaveBeenCalled();
  });

  it('rejects a missing email with 400', async () => {
    const res = await POST(makeRequest({}));
    expect(res.status).toBe(400);
    expect(mockUpsert).not.toHaveBeenCalled();
  });

  it('rejects a non-string email with 400', async () => {
    const res = await POST(makeRequest({ email: 12345 }));
    expect(res.status).toBe(400);
    expect(mockUpsert).not.toHaveBeenCalled();
  });

  it('rejects an over-length email with 400 before any DB call', async () => {
    const huge = `${'a'.repeat(400)}@example.com`;
    const res = await POST(makeRequest({ email: huge }));
    expect(res.status).toBe(400);
    expect(mockCreateServiceRoleClient).not.toHaveBeenCalled();
  });

  it('returns 400 on a malformed JSON body', async () => {
    const res = await POST(makeRequest('{not json'));
    expect(res.status).toBe(400);
    expect(mockUpsert).not.toHaveBeenCalled();
  });

  it('returns 500 (generic) when the DB insert fails', async () => {
    mockUpsert.mockResolvedValueOnce({ error: { message: 'boom' } });
    const res = await POST(makeRequest({ email: 'fail@example.com' }));
    expect(res.status).toBe(500);
    const body = (await res.json()) as { error?: string };
    expect(body.error).toBeTruthy();
    // No internal detail leaked.
    expect(body.error).not.toContain('boom');
  });

  it('rate-limits a single IP after the budget is exhausted', async () => {
    const ip = '203.0.113.7';
    // Limit is 5 per window.
    for (let i = 0; i < 5; i += 1) {
      const res = await POST(makeRequest({ email: `ok${i}@example.com` }, ip));
      expect(res.status).toBe(200);
    }
    const blocked = await POST(makeRequest({ email: 'six@example.com' }, ip));
    expect(blocked.status).toBe(429);
    expect(blocked.headers.get('Retry-After')).toBeTruthy();
  });
});
