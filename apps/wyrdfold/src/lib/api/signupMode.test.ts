/**
 * @jest-environment node
 */
import { readSignupMode } from './signupMode';

const API_URL = 'http://api.test';
const mockFetch = jest.fn();

function upstream(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

beforeEach(() => {
  jest.clearAllMocks();
  process.env['WYRDFOLD_API_URL'] = API_URL;
  delete process.env['WYRDFOLD_BFF_SECRET'];
  global.fetch = mockFetch as unknown as typeof fetch;
  mockFetch.mockResolvedValue(upstream({ mode: 'open' }));
});

describe('readSignupMode', () => {
  // #839 — the regression guard. The landing page held its own copy of this
  // fetch WITHOUT the secret, so `require_bff_secret` 403'd it and the
  // fail-safe below reported 'closed' forever: the homepage could never
  // advertise signup even with the operator switch open. This assertion fails
  // if the header is ever dropped again.
  it('sends the BFF shared secret when WYRDFOLD_BFF_SECRET is set', async () => {
    process.env['WYRDFOLD_BFF_SECRET'] = 'sekret';
    await readSignupMode();
    const [, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get('x-wyrdfold-bff')).toBe('sekret');
  });

  it('omits the secret header when WYRDFOLD_BFF_SECRET is unset', async () => {
    await readSignupMode();
    const [, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get('x-wyrdfold-bff')).toBeNull();
  });

  it('hits the backend signup-mode endpoint', async () => {
    await readSignupMode();
    const [url] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${API_URL}/signup-mode`);
  });

  it('relays an open perimeter', async () => {
    await expect(readSignupMode()).resolves.toBe('open');
  });

  // The exact production failure: a 403 from `require_bff_secret`. It must
  // still fail safe (never advertise signup the perimeter would refuse) —
  // this asserts the fail-safe, so the header assertion above is what proves
  // we don't *reach* this state in the first place.
  it('fails safe to closed on a 403 from the BFF gate', async () => {
    mockFetch.mockResolvedValue(upstream({ detail: 'Forbidden' }, 403));
    await expect(readSignupMode()).resolves.toBe('closed');
  });

  it('fails safe to closed on an unknown mode value', async () => {
    mockFetch.mockResolvedValue(upstream({ mode: 'banana' }));
    await expect(readSignupMode()).resolves.toBe('closed');
  });

  it('fails safe to closed when the backend is unreachable', async () => {
    mockFetch.mockRejectedValue(new Error('ECONNREFUSED'));
    await expect(readSignupMode()).resolves.toBe('closed');
  });

  it('fails safe to closed when WYRDFOLD_API_URL is unset (no fetch)', async () => {
    delete process.env['WYRDFOLD_API_URL'];
    await expect(readSignupMode()).resolves.toBe('closed');
    expect(mockFetch).not.toHaveBeenCalled();
  });
});
