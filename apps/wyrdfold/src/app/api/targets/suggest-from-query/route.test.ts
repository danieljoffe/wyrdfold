/**
 * @jest-environment node
 *
 * BFF for the catalog-search LLM fallback. A thin forwarder to
 * `POST /targets/suggest-from-query`, but it owns a cheap query pre-check
 * (non-blank, ≤200 chars) before spending an upstream LLM round-trip, so pin
 * both the validation gate and the forward shape.
 */
import { NextResponse } from 'next/server';

const mockProxy = jest.fn();

jest.mock('@/lib/api/proxy', () => {
  const { NextResponse: NR } = require('next/server');
  return {
    proxyToWyrdfoldAPI: (...args: unknown[]) => mockProxy(...args),
    LLM_TIMEOUT_MS: 120_000,
    // Real-ish body parser (the module's actual one imports supabase; avoid
    // that by reimplementing the tiny try/catch here).
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
  return new Request('http://localhost/api/targets/suggest-from-query', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: typeof body === 'string' ? body : JSON.stringify(body),
  });
}

describe('POST /api/targets/suggest-from-query (BFF)', () => {
  beforeEach(() => {
    mockProxy.mockReset();
    mockProxy.mockResolvedValue(NextResponse.json({ matches: [] }));
  });

  it('forwards a valid query with POST + the long LLM timeout', async () => {
    await POST(post({ query: 'senior frontend engineer' }));
    expect(mockProxy).toHaveBeenCalledWith(
      '/targets/suggest-from-query',
      expect.objectContaining({
        method: 'POST',
        body: { query: 'senior frontend engineer' },
        timeoutMs: 120_000,
      })
    );
  });

  it('trims the query before forwarding', async () => {
    await POST(post({ query: '  data scientist  ' }));
    const [, opts] = mockProxy.mock.calls[0] as [string, { body: unknown }];
    expect(opts.body).toEqual({ query: 'data scientist' });
  });

  it('rejects a blank / whitespace-only query with 400 (no upstream call)', async () => {
    for (const q of ['', '   ']) {
      mockProxy.mockClear();
      const res = await POST(post({ query: q }));
      expect(res.status).toBe(400);
      expect(mockProxy).not.toHaveBeenCalled();
    }
  });

  it('rejects a missing / non-string query with 400 (no upstream call)', async () => {
    for (const body of [{}, { query: 12345 }, { query: null }]) {
      mockProxy.mockClear();
      const res = await POST(post(body));
      expect(res.status).toBe(400);
      expect(mockProxy).not.toHaveBeenCalled();
    }
  });

  it('rejects an over-length query with 400 (no upstream call)', async () => {
    const res = await POST(post({ query: 'x'.repeat(201) }));
    expect(res.status).toBe(400);
    expect(mockProxy).not.toHaveBeenCalled();
  });

  it('returns 400 on a malformed JSON body (no upstream call)', async () => {
    const res = await POST(post('{not json'));
    expect(res.status).toBe(400);
    expect(mockProxy).not.toHaveBeenCalled();
  });
});
