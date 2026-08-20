/**
 * @jest-environment node
 *
 * The checkout-session BFF forwards the posted body to the upstream authed
 * `POST /billing/checkout-session`.
 *
 * `return_to` (#887) is what makes paying from the onboarding wizard land back
 * in the wizard rather than on /settings. It only works if it survives this
 * hop — and a BFF that rebuilds the body field-by-field silently drops
 * whatever it wasn't updated for, which is exactly how the authed salary
 * filter lost `salary_floor` (#531). These pin the seam.
 */
import { NextRequest } from 'next/server';

const mockProxy = jest.fn();
jest.mock('@/lib/api/proxy', () => {
  const actual =
    jest.requireActual<typeof import('@/lib/api/proxy')>('@/lib/api/proxy');
  return {
    proxyToWyrdfoldAPI: (...args: unknown[]) => mockProxy(...args),
    // Reuse the real body parser so the 400-on-bad-JSON path is genuinely tested.
    readJsonBody: actual.readJsonBody,
  };
});

import { POST } from './route';

function post(body: string): NextRequest {
  return new NextRequest('http://localhost:3100/api/billing/checkout-session', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body,
  });
}

function forwardedBody(): Record<string, unknown> {
  const [path, opts] = mockProxy.mock.calls[0] as [
    string,
    { method: string; body: unknown },
  ];
  expect(path).toBe('/billing/checkout-session');
  expect(opts.method).toBe('POST');
  return opts.body as Record<string, unknown>;
}

describe('POST /api/billing/checkout-session (BFF forwarding)', () => {
  beforeEach(() => {
    mockProxy.mockReset();
    mockProxy.mockResolvedValue(
      new Response(JSON.stringify({ url: 'https://checkout.stripe.com/s' }), {
        status: 200,
      })
    );
  });

  it('forwards return_to alongside plan', async () => {
    await POST(
      post(JSON.stringify({ plan: 'starter', return_to: 'onboarding' }))
    );

    expect(mockProxy).toHaveBeenCalledTimes(1);
    expect(forwardedBody()).toEqual({
      plan: 'starter',
      return_to: 'onboarding',
    });
  });

  it('forwards a body with no return_to unchanged', async () => {
    // The settings card still posts the old shape. The BFF must not invent a
    // value for it — the API's own default is what decides the destination.
    await POST(post(JSON.stringify({ plan: 'pro' })));

    const body = forwardedBody();
    expect(body).toEqual({ plan: 'pro' });
    expect('return_to' in body).toBe(false);
  });

  it('does not filter the body down to fields it knows about', async () => {
    // The real regression guard. A BFF that reconstructs the body would pass
    // this suite's first two cases while dropping the NEXT field someone adds
    // upstream. Proving an unrelated key survives proves the body is
    // forwarded whole rather than allow-listed.
    await POST(
      post(JSON.stringify({ plan: 'starter', a_future_field: 'kept' }))
    );

    expect(forwardedBody()).toEqual({
      plan: 'starter',
      a_future_field: 'kept',
    });
  });

  it('rejects a malformed body before any upstream round-trip', async () => {
    const res = await POST(post('not json'));

    expect(res.status).toBe(400);
    expect(mockProxy).not.toHaveBeenCalled();
  });
});
