/**
 * @jest-environment node
 *
 * BFF for creating-or-linking a target from an AI search-suggestion. A thin
 * forwarder to `POST /targets/from-suggestion`, but it owns a label pre-check
 * and description cap before the upstream (deferred-LLM) round-trip.
 */
import { NextResponse } from 'next/server';

const mockProxy = jest.fn();

jest.mock('@/lib/api/proxy', () => {
  const { NextResponse: NR } = require('next/server');
  return {
    proxyToWyrdfoldAPI: (...args: unknown[]) => mockProxy(...args),
    LLM_TIMEOUT_MS: 120_000,
    readJsonBody: async (req: Request) => {
      try {
        return { ok: true, body: await req.json() };
      } catch {
        return {
          ok: false,
          response: NR.json({ error: 'Invalid JSON body' }, { status: 400 }),
        };
      }
    },
  };
});

import { POST } from './route';

function post(body: unknown): Request {
  return new Request('http://localhost/api/targets/from-suggestion', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: typeof body === 'string' ? body : JSON.stringify(body),
  });
}

describe('POST /api/targets/from-suggestion (BFF)', () => {
  beforeEach(() => {
    mockProxy.mockReset();
    mockProxy.mockResolvedValue(NextResponse.json({ was_matched: false }));
  });

  it('forwards {label, description} with POST + the long LLM timeout', async () => {
    await POST(
      post({
        label: 'Senior Frontend Engineer',
        description: 'Frontend roles.',
      })
    );
    expect(mockProxy).toHaveBeenCalledWith(
      '/targets/from-suggestion',
      expect.objectContaining({
        method: 'POST',
        body: {
          label: 'Senior Frontend Engineer',
          description: 'Frontend roles.',
        },
        timeoutMs: 120_000,
      })
    );
  });

  it('trims the label and forwards an absent description as undefined', async () => {
    await POST(post({ label: '  Staff Engineer  ' }));
    const [, opts] = mockProxy.mock.calls[0] as [string, { body: unknown }];
    expect(opts.body).toEqual({
      label: 'Staff Engineer',
      description: undefined,
    });
  });

  it('caps an over-length description at 500 chars', async () => {
    await POST(post({ label: 'X', description: 'd'.repeat(900) }));
    const [, opts] = mockProxy.mock.calls[0] as [
      string,
      { body: { description: string } },
    ];
    expect(opts.body.description).toHaveLength(500);
  });

  it('rejects a blank / missing / non-string label with 400 (no upstream call)', async () => {
    for (const body of [{}, { label: '' }, { label: '   ' }, { label: 42 }]) {
      mockProxy.mockClear();
      const res = await POST(post(body));
      expect(res.status).toBe(400);
      expect(mockProxy).not.toHaveBeenCalled();
    }
  });

  it('rejects an over-length label with 400 (no upstream call)', async () => {
    const res = await POST(post({ label: 'x'.repeat(201) }));
    expect(res.status).toBe(400);
    expect(mockProxy).not.toHaveBeenCalled();
  });

  it('returns 400 on a malformed JSON body (no upstream call)', async () => {
    const res = await POST(post('{not json'));
    expect(res.status).toBe(400);
    expect(mockProxy).not.toHaveBeenCalled();
  });
});
