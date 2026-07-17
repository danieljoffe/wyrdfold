/**
 * @jest-environment node
 */
import { GET } from './route';

const API_URL = 'http://api.test';
const mockFetch = jest.fn();

function upstream(mode: string): Response {
  return new Response(JSON.stringify({ mode }), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });
}

beforeEach(() => {
  jest.clearAllMocks();
  process.env['WYRDFOLD_API_URL'] = API_URL;
  delete process.env['WYRDFOLD_BFF_SECRET'];
  global.fetch = mockFetch as unknown as typeof fetch;
  mockFetch.mockResolvedValue(upstream('open'));
});

describe('GET /api/signup-mode (BFF forwarder)', () => {
  // SEC-5: the backend requires the BFF secret on this public endpoint.
  it('forwards the BFF shared secret when WYRDFOLD_BFF_SECRET is set', async () => {
    process.env['WYRDFOLD_BFF_SECRET'] = 'sekret';
    await GET();
    const [, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get('x-wyrdfold-bff')).toBe('sekret');
  });

  it('omits the secret header when WYRDFOLD_BFF_SECRET is unset', async () => {
    await GET();
    const [, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get('x-wyrdfold-bff')).toBeNull();
  });

  it('relays the backend mode', async () => {
    const res = await GET();
    await expect(res.json()).resolves.toEqual({ mode: 'open' });
  });

  it('fails safe to closed when WYRDFOLD_API_URL is unset (no fetch)', async () => {
    delete process.env['WYRDFOLD_API_URL'];
    const res = await GET();
    await expect(res.json()).resolves.toEqual({ mode: 'closed' });
    expect(mockFetch).not.toHaveBeenCalled();
  });
});
