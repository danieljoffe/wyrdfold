/**
 * @jest-environment node
 */
import { NextRequest } from 'next/server';
import { POST } from './route';

// Mirror the sibling public-search route spec: stub the secret + IP helpers so
// the assertions pin THIS route's forwarding behavior, not the helpers'.
jest.mock('@/lib/api/bffSecret', () => ({
  bffSecretHeader: () => ({ 'X-Wyrdfold-BFF': 'test-secret' }),
}));
jest.mock('@/lib/api/clientIp', () => ({
  clientIp: jest.fn(() => '203.0.113.9'),
}));

const API_URL = 'http://api.test';

function beaconRequest(body: string): NextRequest {
  return new NextRequest('http://localhost:3100/api/search-events', {
    method: 'POST',
    body,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('POST /api/search-events (funnel beacon BFF)', () => {
  const fetchMock = jest.fn();
  const origEnv = process.env['WYRDFOLD_API_URL'];

  beforeEach(() => {
    fetchMock
      .mockReset()
      .mockResolvedValue(new Response(null, { status: 204 }));
    global.fetch = fetchMock as unknown as typeof fetch;
    process.env['WYRDFOLD_API_URL'] = API_URL;
  });

  afterAll(() => {
    process.env['WYRDFOLD_API_URL'] = origEnv;
  });

  it('forwards the body with the BFF secret + trusted IP and NO Bearer', async () => {
    const body = JSON.stringify({
      event_type: 'card_open',
      surface: 'public',
      job_posting_id: '11111111-1111-1111-1111-111111111111',
    });
    const res = await POST(beaconRequest(body));

    expect(res.status).toBe(204);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${API_URL}/search-events`);
    expect(init.method).toBe('POST');
    expect(init.body).toBe(body);
    const headers = init.headers as Record<string, string>;
    expect(headers['X-Wyrdfold-BFF']).toBe('test-secret');
    expect(headers['x-forwarded-for']).toBe('203.0.113.9');
    // The beacon is identity-free by design — never a user credential.
    expect(headers['Authorization']).toBeUndefined();
    expect(headers['authorization']).toBeUndefined();
  });

  it('answers 204 even when the upstream call fails (fire-and-forget)', async () => {
    fetchMock.mockRejectedValue(new Error('upstream down'));
    const res = await POST(
      beaconRequest(
        JSON.stringify({ event_type: 'card_open', surface: 'public' })
      )
    );
    expect(res.status).toBe(204);
  });

  it('answers 204 and skips the upstream when WYRDFOLD_API_URL is unset', async () => {
    delete process.env['WYRDFOLD_API_URL'];
    const res = await POST(
      beaconRequest(
        JSON.stringify({ event_type: 'card_open', surface: 'public' })
      )
    );
    expect(res.status).toBe(204);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('rejects an oversized body without forwarding it (413)', async () => {
    const res = await POST(beaconRequest('x'.repeat(2048)));
    expect(res.status).toBe(413);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
