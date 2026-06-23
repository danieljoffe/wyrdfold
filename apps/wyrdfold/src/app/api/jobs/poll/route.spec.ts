/**
 * @jest-environment node
 *
 * Tests for the cron-only global poll trigger (`POST /api/jobs/poll`).
 *
 * The route force-polls every enabled source across all tenants via the
 * upstream operator-key-gated `/poll`. The only legitimate caller is the Vercel
 * cron presenting `CRON_SECRET`. Audit #29: the route used to also accept any
 * authenticated user session, letting any logged-in user trigger the global
 * all-tenant poll. These tests pin the cron-only contract so the regression
 * can't come back, and pin that the BFF forwards ONLY the narrow
 * `WYRDFOLD_CRON_KEY` (no fallback to the broad `WYRDFOLD_API_KEY`).
 */
import { NextRequest } from 'next/server';

const mockGetAccessToken = jest.fn();

// If the route ever re-introduces a session path it would import this; the
// mock lets us assert it is NEVER consulted.
jest.mock('@/lib/api/proxy', () => ({
  getAccessToken: (...args: unknown[]) => mockGetAccessToken(...args),
}));

import { POST } from './route';

const CRON = 'test-cron-secret';
const CRON_KEY = 'test-cron-key';
const BASE_URL = 'http://api.test';
const ENV_KEYS = [
  'CRON_SECRET',
  'WYRDFOLD_API_KEY',
  'WYRDFOLD_CRON_KEY',
  'WYRDFOLD_API_URL',
];

function setEnv(name: string, value: string | undefined): void {
  if (value === undefined) {
    delete process.env[name];
  } else {
    process.env[name] = value;
  }
}

function req(authHeader?: string): NextRequest {
  const headers = new Headers();
  if (authHeader !== undefined) headers.set('authorization', authHeader);
  return new NextRequest('http://localhost:3100/api/jobs/poll', {
    method: 'POST',
    headers,
  });
}

describe('POST /api/jobs/poll (cron-only)', () => {
  const original: Record<string, string | undefined> = {};
  const realFetch = global.fetch;

  beforeEach(() => {
    for (const k of ENV_KEYS) original[k] = process.env[k];
    setEnv('CRON_SECRET', CRON);
    setEnv('WYRDFOLD_CRON_KEY', CRON_KEY);
    setEnv('WYRDFOLD_API_KEY', undefined);
    setEnv('WYRDFOLD_API_URL', BASE_URL);
    mockGetAccessToken.mockReset();
    global.fetch = jest.fn();
  });

  afterEach(() => {
    for (const k of ENV_KEYS) setEnv(k, original[k]);
    global.fetch = realFetch;
  });

  it('forwards to upstream /poll with the cron key when the cron secret matches', async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce(
      new Response(JSON.stringify({ sources_polled: 3, new_jobs: 7 }), {
        status: 200,
      })
    );

    const res = await POST(req(`Bearer ${CRON}`));

    expect(res.status).toBe(200);
    await expect(res.json()).resolves.toEqual({
      sources_polled: 3,
      new_jobs: 7,
    });
    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [url, init] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${BASE_URL}/poll`);
    expect((init as RequestInit).method).toBe('POST');
    expect(
      (init as { headers: Record<string, string> }).headers['x-api-key']
    ).toBe(CRON_KEY);
    // The session path must be gone — getAccessToken is never consulted.
    expect(mockGetAccessToken).not.toHaveBeenCalled();
  });

  it('does NOT fall back to WYRDFOLD_API_KEY — cron key only (#29 migration)', async () => {
    setEnv('WYRDFOLD_CRON_KEY', undefined);
    setEnv('WYRDFOLD_API_KEY', 'the-broad-legacy-key');

    const res = await POST(req(`Bearer ${CRON}`));

    // No cron key -> 503, even though the broad legacy key is present.
    expect(res.status).toBe(503);
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('rejects an authenticated user session (no cron secret) with 403 — the priv-esc fix', async () => {
    // An attacker with a valid Supabase session but no cron secret.
    mockGetAccessToken.mockResolvedValue('a-valid-user-jwt');

    const res = await POST(req('Bearer not-the-cron-secret'));

    expect(res.status).toBe(403);
    // Never hits upstream — no global all-tenant poll triggered.
    expect(global.fetch).not.toHaveBeenCalled();
    // The user session is never even checked; auth is cron-secret-only now.
    expect(mockGetAccessToken).not.toHaveBeenCalled();
  });

  it('rejects a request with no authorization header with 403', async () => {
    const res = await POST(req());

    expect(res.status).toBe(403);
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('rejects a wrong-length presented secret with 403 (constant-time compare)', async () => {
    const res = await POST(req('Bearer x'));

    expect(res.status).toBe(403);
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('fails closed (503) when CRON_SECRET is not configured — no probe into the privileged path', async () => {
    setEnv('CRON_SECRET', undefined);

    // Even presenting an empty Bearer (which would match an empty secret if
    // we compared naively) must not get through.
    const res = await POST(req('Bearer '));

    expect(res.status).toBe(503);
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('returns 503 when WYRDFOLD_CRON_KEY is missing even for a valid cron call', async () => {
    setEnv('WYRDFOLD_CRON_KEY', undefined);

    const res = await POST(req(`Bearer ${CRON}`));

    expect(res.status).toBe(503);
    expect(global.fetch).not.toHaveBeenCalled();
  });
});
