/**
 * @jest-environment node
 *
 * The activate proxy must FORWARD its body.
 *
 * This route previously ignored the request entirely (`_request`, no body
 * passed on), which was correct while activation took no input and silently
 * wrong the moment it did. With the swap, dropping the body means the upstream
 * sees a plain activation and returns the very 409 the swap exists to resolve
 * — a failure with no error anywhere, in either log. The same BFF layer
 * dropped a salary filter this way before, so it gets a test rather than a
 * reading.
 */
import { NextRequest } from 'next/server';

const mockProxy = jest.fn();

jest.mock('@/lib/api/proxy', () => ({
  proxyToWyrdfoldAPI: (...args: unknown[]) => mockProxy(...args),
  readJsonBody: async (req: Request) => {
    try {
      return { ok: true as const, body: await req.json() };
    } catch {
      return {
        ok: false as const,
        response: new Response(null, { status: 400 }),
      };
    }
  },
  LLM_TIMEOUT_MS: 120_000,
}));

import { POST as activate } from './activate/route';

const ctx = (id: string) => ({ params: Promise.resolve({ id }) });

describe('POST /api/targets/[id]/activate', () => {
  beforeEach(() => {
    mockProxy.mockReset();
    mockProxy.mockResolvedValue(undefined);
  });

  it('forwards a swap body to the upstream activate route', async () => {
    const req = new NextRequest('http://localhost/api/targets/t-new/activate', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ deactivate_target_id: 't-old' }),
    });

    await activate(req, ctx('t-new'));

    expect(mockProxy).toHaveBeenCalledTimes(1);
    const [path, options] = mockProxy.mock.calls[0] as [
      string,
      { method: string; body?: unknown },
    ];
    expect(path).toBe('/targets/t-new/activate');
    expect(options.method).toBe('POST');
    // The assertion that would have caught the original bug.
    expect(options.body).toEqual({ deactivate_target_id: 't-old' });
  });

  it('still works with no body at all (a plain activation)', async () => {
    const req = new NextRequest('http://localhost/api/targets/t-new/activate', {
      method: 'POST',
    });

    await activate(req, ctx('t-new'));

    expect(mockProxy).toHaveBeenCalledTimes(1);
    const [path, options] = mockProxy.mock.calls[0] as [
      string,
      { method: string; body?: unknown },
    ];
    expect(path).toBe('/targets/t-new/activate');
    expect(options.method).toBe('POST');
    // Not merely undefined-valued — absent, so the upstream sees no body.
    expect('body' in options).toBe(false);
  });
});
