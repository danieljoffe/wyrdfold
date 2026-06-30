/**
 * @jest-environment node
 *
 * Pins the BFF → upstream path mapping for the feedback learning-loop routes
 * (#79). The risky part of these thin proxies is the path construction —
 * especially the nested `[run_id]` apply/reject segments and the
 * learning-log query passthrough — so we assert each handler forwards to the
 * exact upstream path with the right options.
 */
import { NextRequest } from 'next/server';

const mockProxy = jest.fn();

jest.mock('@/lib/api/proxy', () => ({
  proxyToWyrdfoldAPI: (...args: unknown[]) => mockProxy(...args),
  LLM_TIMEOUT_MS: 120_000,
}));

import { GET as getLearningLog } from './learning-log/route';
import { POST as postLearnLlm } from './learn-llm/route';
import { POST as postApply } from './learn/[run_id]/apply/route';
import { POST as postReject } from './learn/[run_id]/reject/route';

const idCtx = (id: string) => ({ params: Promise.resolve({ id }) });
const runCtx = (id: string, run_id: string) => ({
  params: Promise.resolve({ id, run_id }),
});

function post(url: string): NextRequest {
  return new NextRequest(url, { method: 'POST' });
}

describe('learning-loop BFF routes', () => {
  beforeEach(() => {
    mockProxy.mockReset();
    mockProxy.mockResolvedValue(undefined);
  });

  it('GET learning-log forwards the path and passes the query through', async () => {
    const req = new NextRequest(
      'http://localhost/api/targets/t1/learning-log?status=staged&limit=50'
    );
    await getLearningLog(req, idCtx('t1'));

    expect(mockProxy).toHaveBeenCalledTimes(1);
    const [path, opts] = mockProxy.mock.calls[0] as [
      string,
      { searchParams: URLSearchParams },
    ];
    expect(path).toBe('/targets/t1/learning-log');
    expect(opts.searchParams.get('status')).toBe('staged');
    expect(opts.searchParams.get('limit')).toBe('50');
  });

  it('POST learn-llm forwards with the longer LLM timeout', async () => {
    await postLearnLlm(
      post('http://localhost/api/targets/t1/learn-llm'),
      idCtx('t1')
    );

    expect(mockProxy).toHaveBeenCalledWith(
      '/targets/t1/learn-llm',
      expect.objectContaining({ method: 'POST', timeoutMs: 120_000 })
    );
  });

  it('POST apply forwards the nested run_id path', async () => {
    await postApply(
      post('http://localhost/api/targets/t1/learn/run-9/apply'),
      runCtx('t1', 'run-9')
    );

    expect(mockProxy).toHaveBeenCalledWith(
      '/targets/t1/learn/run-9/apply',
      expect.objectContaining({ method: 'POST' })
    );
  });

  it('POST reject forwards the nested run_id path', async () => {
    await postReject(
      post('http://localhost/api/targets/t1/learn/run-9/reject'),
      runCtx('t1', 'run-9')
    );

    expect(mockProxy).toHaveBeenCalledWith(
      '/targets/t1/learn/run-9/reject',
      expect.objectContaining({ method: 'POST' })
    );
  });
});
