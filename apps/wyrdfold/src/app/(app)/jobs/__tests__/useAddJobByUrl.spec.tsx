import { renderHook, act } from '@testing-library/react';
import { useAddJobByUrl } from '../useAddJobByUrl';

const mockToast = jest.fn();
jest.mock('@/state/Toast/ToastProvider', () => ({
  useToast: () => ({ toast: mockToast }),
}));
jest.mock('@/lib/extractApiError', () => ({
  extractApiError: jest.fn(async (_res: unknown, fallback: string) => fallback),
}));

const ORIGINAL_FETCH = global.fetch;
const ORIGINAL_PROMPT = window.prompt;
beforeEach(() => jest.clearAllMocks());
afterAll(() => {
  global.fetch = ORIGINAL_FETCH;
  window.prompt = ORIGINAL_PROMPT;
});

describe('useAddJobByUrl', () => {
  it('POSTs the trimmed URL, toasts success, and calls onJobAdded', async () => {
    window.prompt = jest.fn().mockReturnValue('  https://x.com/job  ');
    global.fetch = jest
      .fn()
      .mockResolvedValue({ ok: true }) as unknown as typeof fetch;
    const onJobAdded = jest.fn();

    const { result } = renderHook(() => useAddJobByUrl(onJobAdded));
    await act(async () => {
      await result.current.addJobByUrl();
    });

    expect(global.fetch).toHaveBeenCalledWith(
      '/api/jobs/manual',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ url: 'https://x.com/job' }),
      })
    );
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({ variant: 'success', title: 'Job added' })
    );
    expect(onJobAdded).toHaveBeenCalled();
    expect(result.current.submitting).toBe(false);
  });

  it('is a no-op when the prompt is cancelled (null)', async () => {
    window.prompt = jest.fn().mockReturnValue(null);
    global.fetch = jest.fn() as unknown as typeof fetch;
    const onJobAdded = jest.fn();

    const { result } = renderHook(() => useAddJobByUrl(onJobAdded));
    await act(async () => {
      await result.current.addJobByUrl();
    });

    expect(global.fetch).not.toHaveBeenCalled();
    expect(onJobAdded).not.toHaveBeenCalled();
  });

  it('is a no-op when the prompt is blank/whitespace', async () => {
    window.prompt = jest.fn().mockReturnValue('   ');
    global.fetch = jest.fn() as unknown as typeof fetch;

    const { result } = renderHook(() => useAddJobByUrl(jest.fn()));
    await act(async () => {
      await result.current.addJobByUrl();
    });

    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('toasts an error and skips onJobAdded on a non-OK response', async () => {
    window.prompt = jest.fn().mockReturnValue('https://x.com/job');
    global.fetch = jest
      .fn()
      .mockResolvedValue({ ok: false, status: 422 }) as unknown as typeof fetch;
    const onJobAdded = jest.fn();

    const { result } = renderHook(() => useAddJobByUrl(onJobAdded));
    await act(async () => {
      await result.current.addJobByUrl();
    });

    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({ variant: 'error', title: 'Could not add job' })
    );
    expect(onJobAdded).not.toHaveBeenCalled();
  });

  it('toasts a network error on a thrown fetch', async () => {
    window.prompt = jest.fn().mockReturnValue('https://x.com/job');
    global.fetch = jest
      .fn()
      .mockRejectedValue(new Error('offline')) as unknown as typeof fetch;
    const onJobAdded = jest.fn();

    const { result } = renderHook(() => useAddJobByUrl(onJobAdded));
    await act(async () => {
      await result.current.addJobByUrl();
    });

    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({
        variant: 'error',
        title: 'Network error adding job',
      })
    );
    expect(onJobAdded).not.toHaveBeenCalled();
  });
});
