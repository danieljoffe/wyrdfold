import { renderHook, act } from '@testing-library/react';
import { useJobRemove } from '../useJobRemove';

const mockToast = jest.fn();
jest.mock('@/state/Toast/ToastProvider', () => ({
  useToast: () => ({ toast: mockToast }),
}));
jest.mock('@/lib/extractApiError', () => ({
  extractApiError: jest.fn(async (_res: unknown, fallback: string) => fallback),
}));

const ORIGINAL_FETCH = global.fetch;
beforeEach(() => jest.clearAllMocks());
afterAll(() => {
  global.fetch = ORIGINAL_FETCH;
});

function okFetch() {
  return jest.fn().mockResolvedValue({ ok: true, status: 200 });
}

describe('useJobRemove', () => {
  it('POSTs the target in scope so removal stays per-target', async () => {
    global.fetch = okFetch() as unknown as typeof fetch;
    const { result } = renderHook(() => useJobRemove());

    await act(async () => {
      await result.current.removeJobs(['j1', 'j2'], 't1');
    });

    expect(global.fetch).toHaveBeenCalledWith(
      '/api/jobs/j1/remove',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ target_id: 't1' }),
      })
    );
    expect(global.fetch).toHaveBeenCalledTimes(2);
  });

  it('sends a null target on All Jobs so the API clears every holding target', async () => {
    global.fetch = okFetch() as unknown as typeof fetch;
    const { result } = renderHook(() => useJobRemove());

    await act(async () => {
      await result.current.removeJobs(['j1'], undefined);
    });

    expect(global.fetch).toHaveBeenCalledWith(
      '/api/jobs/j1/remove',
      expect.objectContaining({ body: JSON.stringify({ target_id: null }) })
    );
  });

  // The old delete flow claimed "This can't be undone" and offered nothing.
  it('offers Undo on the success toast', async () => {
    global.fetch = okFetch() as unknown as typeof fetch;
    const { result } = renderHook(() => useJobRemove());

    await act(async () => {
      await result.current.removeJobs(['j1'], 't1');
    });

    const call = mockToast.mock.calls[0]?.[0];
    expect(call.variant).toBe('success');
    expect(call.title).toMatch(/removed 1 job/i);
    expect(call.action?.label).toBe('Undo');
  });

  it('Undo DELETEs the removal and reports what was restored', async () => {
    global.fetch = okFetch() as unknown as typeof fetch;
    const onUndone = jest.fn();
    const { result } = renderHook(() => useJobRemove());

    await act(async () => {
      await result.current.removeJobs(['j1'], 't1', onUndone);
    });
    (global.fetch as jest.Mock).mockClear();

    const action = mockToast.mock.calls[0]?.[0].action;
    await act(async () => {
      action.onClick();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(global.fetch).toHaveBeenCalledWith(
      '/api/jobs/j1/remove',
      expect.objectContaining({ method: 'DELETE' })
    );
  });

  it('reports an error and no success toast when every removal fails', async () => {
    global.fetch = jest
      .fn()
      .mockResolvedValue({ ok: false, status: 404 }) as unknown as typeof fetch;
    const { result } = renderHook(() => useJobRemove());

    let removed = -1;
    await act(async () => {
      removed = await result.current.removeJobs(['j1'], 't1');
    });

    expect(removed).toBe(0);
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({ variant: 'error' })
    );
    expect(mockToast).not.toHaveBeenCalledWith(
      expect.objectContaining({ variant: 'success' })
    );
  });

  it('counts only the ones that actually succeeded on a partial failure', async () => {
    global.fetch = jest
      .fn()
      .mockResolvedValueOnce({ ok: true, status: 200 })
      .mockResolvedValueOnce({
        ok: false,
        status: 500,
      }) as unknown as typeof fetch;
    const { result } = renderHook(() => useJobRemove());

    let removed = -1;
    await act(async () => {
      removed = await result.current.removeJobs(['j1', 'j2'], 't1');
    });

    expect(removed).toBe(1);
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({
        title: expect.stringMatching(/removed 1 job/i),
      })
    );
  });

  it('is a no-op for an empty selection', async () => {
    global.fetch = okFetch() as unknown as typeof fetch;
    const { result } = renderHook(() => useJobRemove());

    await act(async () => {
      await result.current.removeJobs([], 't1');
    });

    expect(global.fetch).not.toHaveBeenCalled();
  });
});
