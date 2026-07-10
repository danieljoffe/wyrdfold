import { renderHook, act } from '@testing-library/react';
import { useJobDelete } from '../useJobDelete';

const mockToast = jest.fn();
jest.mock('@/state/Toast/ToastProvider', () => ({
  useToast: () => ({ toast: mockToast }),
}));

// Deterministic: extractApiError just returns the caller's fallback, so the
// error-toast copy is exercised without depending on the parser's internals.
jest.mock('@/lib/extractApiError', () => ({
  extractApiError: jest.fn(async (_res: unknown, fallback: string) => fallback),
}));

const ORIGINAL_FETCH = global.fetch;
beforeEach(() => jest.clearAllMocks());
afterAll(() => {
  global.fetch = ORIGINAL_FETCH;
});

describe('useJobDelete', () => {
  it('deleteJob: success → true, DELETEs the job URL, success toast, deleting settles false', async () => {
    global.fetch = jest
      .fn()
      .mockResolvedValue({ ok: true }) as unknown as typeof fetch;

    const { result } = renderHook(() => useJobDelete());
    let ok: boolean | undefined;
    await act(async () => {
      ok = await result.current.deleteJob('j-1');
    });

    expect(ok).toBe(true);
    expect(global.fetch).toHaveBeenCalledWith('/api/jobs/j-1', {
      method: 'DELETE',
    });
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({ variant: 'success', title: 'Job deleted' })
    );
    expect(result.current.deleting).toBe(false);
  });

  it('deleteJob: non-OK → false + error toast (server/fallback message)', async () => {
    global.fetch = jest
      .fn()
      .mockResolvedValue({ ok: false, status: 500 }) as unknown as typeof fetch;

    const { result } = renderHook(() => useJobDelete());
    let ok: boolean | undefined;
    await act(async () => {
      ok = await result.current.deleteJob('j-1');
    });

    expect(ok).toBe(false);
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({
        variant: 'error',
        title: 'Failed to delete job',
      })
    );
  });

  it('deleteJob: network throw → false + distinct network-error toast', async () => {
    global.fetch = jest
      .fn()
      .mockRejectedValue(new Error('offline')) as unknown as typeof fetch;

    const { result } = renderHook(() => useJobDelete());
    let ok: boolean | undefined;
    await act(async () => {
      ok = await result.current.deleteJob('j-1');
    });

    expect(ok).toBe(false);
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({
        variant: 'error',
        title: 'Network error deleting job',
      })
    );
  });

  it('deleteJobs: counts successes across a mixed batch + summary toast', async () => {
    global.fetch = jest
      .fn()
      .mockResolvedValueOnce({ ok: true })
      .mockResolvedValueOnce({ ok: false })
      .mockResolvedValueOnce({ ok: true }) as unknown as typeof fetch;

    const { result } = renderHook(() => useJobDelete());
    let n: number | undefined;
    await act(async () => {
      n = await result.current.deleteJobs(['a', 'b', 'c']);
    });

    expect(n).toBe(2);
    expect(global.fetch).toHaveBeenCalledTimes(3);
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({ variant: 'success', title: 'Deleted 2 jobs' })
    );
  });

  it('deleteJobs: empty list is a no-op (no fetch, returns 0)', async () => {
    global.fetch = jest.fn() as unknown as typeof fetch;

    const { result } = renderHook(() => useJobDelete());
    let n: number | undefined;
    await act(async () => {
      n = await result.current.deleteJobs([]);
    });

    expect(n).toBe(0);
    expect(global.fetch).not.toHaveBeenCalled();
  });
});
