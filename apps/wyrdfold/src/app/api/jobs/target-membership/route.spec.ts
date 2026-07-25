/**
 * @jest-environment node
 *
 * The target-membership BFF forwards the posted body to the upstream authed
 * `POST /jobs/target-membership` (#467 §11). These tests pin the seam: the
 * exact path + method + body pass through, and a malformed body is rejected as
 * a 400 before any upstream round-trip.
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
  return new NextRequest('http://localhost:3100/api/jobs/target-membership', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body,
  });
}

describe('POST /api/jobs/target-membership (BFF forwarding)', () => {
  beforeEach(() => {
    mockProxy.mockReset();
    mockProxy.mockResolvedValue(
      new Response(JSON.stringify({ memberships: {} }), { status: 200 })
    );
  });

  it('forwards the job_posting_ids body to upstream /jobs/target-membership', async () => {
    await POST(post(JSON.stringify({ job_posting_ids: ['a', 'b'] })));

    expect(mockProxy).toHaveBeenCalledTimes(1);
    const [path, opts] = mockProxy.mock.calls[0] as [
      string,
      { method: string; body: unknown },
    ];
    expect(path).toBe('/jobs/target-membership');
    expect(opts.method).toBe('POST');
    expect(opts.body).toEqual({ job_posting_ids: ['a', 'b'] });
  });

  it('400s on a malformed JSON body, without calling upstream', async () => {
    const res = await POST(post('not json'));
    expect(res.status).toBe(400);
    expect(mockProxy).not.toHaveBeenCalled();
  });
});
