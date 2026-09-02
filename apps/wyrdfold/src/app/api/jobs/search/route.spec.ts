/**
 * @jest-environment node
 *
 * The search BFF forwards the query + filters to the upstream authed `/search`.
 *
 * Regression (#467 fast-follow): an earlier version forwarded only
 * `q`/`page_size`/`offset` and silently DROPPED the `location` + recency
 * filters, so the API returned unfiltered results even though the UI reflected
 * the filters as "applied". These tests pin the passthrough so the seam can't
 * quietly break again.
 */
import { NextRequest } from 'next/server';

const mockProxy = jest.fn();
jest.mock('@/lib/api/proxy', () => ({
  proxyToWyrdfoldAPI: (...args: unknown[]) => mockProxy(...args),
}));

import { GET } from './route';

function get(qs: string): NextRequest {
  return new NextRequest(`http://localhost:3100/api/jobs/search?${qs}`);
}

function forwardedParams(): URLSearchParams {
  const [path, opts] = mockProxy.mock.calls[0] as [
    string,
    { searchParams: URLSearchParams },
  ];
  expect(path).toBe('/search');
  return opts.searchParams;
}

describe('GET /api/jobs/search (BFF forwarding)', () => {
  beforeEach(() => {
    mockProxy.mockReset();
    mockProxy.mockResolvedValue(new Response('{}', { status: 200 }));
  });

  it('forwards q, pagination, AND the filters to upstream /search', async () => {
    await GET(
      get(
        'q=engineer&page_size=20&offset=20&location=Remote&posted_within_days=30' +
          '&salary_floor=150000'
      )
    );
    expect(mockProxy).toHaveBeenCalledTimes(1);
    const sp = forwardedParams();
    expect(sp.get('q')).toBe('engineer');
    expect(sp.get('page_size')).toBe('20');
    expect(sp.get('offset')).toBe('20');
    expect(sp.get('location')).toBe('Remote');
    expect(sp.get('posted_within_days')).toBe('30');
    expect(sp.get('salary_floor')).toBe('150000');
  });

  it('forwards EVERY shared filter param — the drift that shipped twice', async () => {
    // Pin against the exact regression class: a filter added to the API +
    // component that one BFF route forwards and the other silently drops.
    const { SEARCH_FILTER_PARAMS } =
      await import('@/lib/api/searchFilterParams');
    const qs = SEARCH_FILTER_PARAMS.map((p, i) => `${p}=v${i}`).join('&');
    await GET(get(`q=engineer&${qs}`));
    const sp = forwardedParams();
    SEARCH_FILTER_PARAMS.forEach((p, i) => {
      expect(sp.get(p)).toBe(`v${i}`);
    });
  });

  it('omits filters that are absent (no empty params leak upstream)', async () => {
    await GET(get('q=engineer'));
    const sp = forwardedParams();
    expect(sp.has('location')).toBe(false);
    expect(sp.has('posted_within_days')).toBe(false);
  });

  it('forwards a missing q as blank — filters-only browse (#834)', async () => {
    const res = await GET(get('location=Remote'));
    expect(res.status).toBe(200);
    const sp = forwardedParams();
    expect(sp.get('q')).toBe('');
    expect(sp.get('location')).toBe('Remote');
  });
});
