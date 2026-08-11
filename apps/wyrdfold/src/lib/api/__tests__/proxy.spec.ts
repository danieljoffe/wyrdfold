/**
 * @jest-environment node
 */
import { proxyToWyrdfoldAPI } from '../proxy';

// Auth is not under test — resolve a token so the proxy reaches fetch.
jest.mock('@/lib/supabase/auth-server', () => ({
  createAuthServerClient: jest.fn(async () => ({
    auth: {
      getUser: jest.fn(async () => ({
        data: { user: { id: 'u-1' } },
        error: null,
      })),
      getSession: jest.fn(async () => ({
        data: { session: { access_token: 'jwt' } },
      })),
    },
  })),
}));

/**
 * Transient-retry contract for the route-handler proxy (#604).
 *
 * Prod evidence 2026-08-05: browser-visible 503s on /api/jobs while
 * Railway logged the same requests 200-but-slow — the upstream finished
 * into a dead socket, the proxy's single fetch rejected, and the user got
 * "WyrdFold API unavailable" for a request that succeeded server-side.
 * GETs are re-issued once; writes never are (a duplicated analysis/tailor
 * POST double-spends).
 */
describe('proxyToWyrdfoldAPI transient retry', () => {
  const okJson = () =>
    new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });

  beforeEach(() => {
    process.env['WYRDFOLD_API_URL'] = 'http://upstream.test';
    global.fetch = jest.fn();
  });

  it('re-issues a GET once after a network-level rejection', async () => {
    (global.fetch as jest.Mock)
      .mockRejectedValueOnce(new TypeError('socket hang up'))
      .mockResolvedValueOnce(okJson());

    const res = await proxyToWyrdfoldAPI('/jobs');

    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true });
    expect(global.fetch).toHaveBeenCalledTimes(2);
  });

  it('re-issues a GET once after an upstream 5xx', async () => {
    (global.fetch as jest.Mock)
      .mockResolvedValueOnce(new Response('bad gateway', { status: 502 }))
      .mockResolvedValueOnce(okJson());

    const res = await proxyToWyrdfoldAPI('/jobs');

    expect(res.status).toBe(200);
    expect(global.fetch).toHaveBeenCalledTimes(2);
  });

  it('returns 503 when the retry also fails, without a third attempt', async () => {
    (global.fetch as jest.Mock)
      .mockRejectedValueOnce(new TypeError('socket hang up'))
      .mockRejectedValueOnce(new TypeError('socket hang up'));

    const res = await proxyToWyrdfoldAPI('/jobs');

    expect(res.status).toBe(503);
    expect(global.fetch).toHaveBeenCalledTimes(2);
  });

  it('forwards the final attempt upstream 5xx body/status as-is', async () => {
    (global.fetch as jest.Mock)
      .mockResolvedValueOnce(new Response('nope', { status: 502 }))
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: 'still down' }), { status: 500 })
      );

    const res = await proxyToWyrdfoldAPI('/jobs');

    expect(res.status).toBe(500);
    expect(await res.json()).toEqual({ detail: 'still down' });
  });

  it('never re-issues a POST', async () => {
    (global.fetch as jest.Mock).mockRejectedValueOnce(
      new TypeError('socket hang up')
    );

    const res = await proxyToWyrdfoldAPI('/analysis/j-1', {
      method: 'POST',
      body: { target_id: 't-1' },
    });

    expect(res.status).toBe(503);
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });

  it('never re-issues a POST on upstream 5xx either', async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: 'boom' }), { status: 500 })
    );

    const res = await proxyToWyrdfoldAPI('/analysis/j-1', { method: 'POST' });

    expect(res.status).toBe(500);
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });
});
