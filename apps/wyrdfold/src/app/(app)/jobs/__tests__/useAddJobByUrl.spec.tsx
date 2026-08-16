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
beforeEach(() => jest.clearAllMocks());
afterAll(() => {
  global.fetch = ORIGINAL_FETCH;
});

/** `POST /jobs/manual` success shape. */
function added(postingId = 'job-1') {
  return {
    ok: true,
    json: async () => ({
      success: true,
      posting_id: postingId,
      extracted: { title: 'X', company_name: 'Y', location: 'Z' },
      extraction_tier: 'jsonld',
      warnings: [],
      needs_manual_fields: false,
    }),
  };
}

/**
 * The shape that used to be reported as success: HTTP 200, nothing created.
 * This is what LinkedIn produces — Firecrawl gets a 403 and the endpoint
 * still answers 200.
 */
function notAdded(warnings: string[] = ['firecrawl_failed:http_403']) {
  return {
    ok: true,
    json: async () => ({
      success: false,
      posting_id: null,
      extracted: { title: null, company_name: null, location: null },
      extraction_tier: 'none',
      warnings,
      needs_manual_fields: true,
    }),
  };
}

describe('useAddJobByUrl', () => {
  it('POSTs the trimmed URL, toasts success, closes, and calls onJobAdded', async () => {
    global.fetch = jest
      .fn()
      .mockResolvedValue(added()) as unknown as typeof fetch;
    const onJobAdded = jest.fn();

    const { result } = renderHook(() => useAddJobByUrl(onJobAdded));
    act(() => result.current.open());
    await act(async () => {
      await result.current.submit({ url: '  https://x.com/job  ' });
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
    expect(result.current.isOpen).toBe(false);
    expect(result.current.error).toBeNull();
  });

  // The regression this hook exists to prevent. Against the previous
  // implementation (which branched on `res.ok` alone) this case toasted
  // "Job added" and fired onJobAdded for a job that was never created.
  it('treats HTTP 200 with success:false as a FAILURE, not an add', async () => {
    global.fetch = jest
      .fn()
      .mockResolvedValue(notAdded()) as unknown as typeof fetch;
    const onJobAdded = jest.fn();

    const { result } = renderHook(() => useAddJobByUrl(onJobAdded));
    await act(async () => {
      await result.current.submit({
        url: 'https://www.linkedin.com/jobs/view/1',
      });
    });

    expect(onJobAdded).not.toHaveBeenCalled();
    expect(mockToast).not.toHaveBeenCalledWith(
      expect.objectContaining({ variant: 'success' })
    );
    expect(result.current.error).toMatch(/blocks automated readers/i);
  });

  it('surfaces the manual-field fallback with whatever was extracted', async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        success: false,
        posting_id: null,
        extracted: {
          title: null,
          company_name: 'Acme',
          location: 'Remote',
        },
        extraction_tier: 'none',
        warnings: ['firecrawl_failed:no_metadata'],
        needs_manual_fields: true,
      }),
    }) as unknown as typeof fetch;

    const { result } = renderHook(() => useAddJobByUrl(jest.fn()));
    await act(async () => {
      await result.current.submit({ url: 'https://x.com/job' });
    });

    expect(result.current.needsManualFields).toBe(true);
    expect(result.current.extracted).toEqual({
      title: null,
      company_name: 'Acme',
      location: 'Remote',
    });
  });

  it('sends manual overrides on the retry submit', async () => {
    global.fetch = jest
      .fn()
      .mockResolvedValue(added()) as unknown as typeof fetch;

    const { result } = renderHook(() => useAddJobByUrl(jest.fn()));
    await act(async () => {
      await result.current.submit({
        url: 'https://x.com/job',
        title: 'Staff Engineer',
        company_name: 'Acme',
        location: 'Remote',
      });
    });

    expect(global.fetch).toHaveBeenCalledWith(
      '/api/jobs/manual',
      expect.objectContaining({
        body: JSON.stringify({
          url: 'https://x.com/job',
          title: 'Staff Engineer',
          company_name: 'Acme',
          location: 'Remote',
        }),
      })
    );
  });

  it('is a no-op when the URL is blank/whitespace', async () => {
    global.fetch = jest.fn() as unknown as typeof fetch;

    const { result } = renderHook(() => useAddJobByUrl(jest.fn()));
    await act(async () => {
      await result.current.submit({ url: '   ' });
    });

    expect(global.fetch).not.toHaveBeenCalled();
    expect(result.current.error).toMatch(/paste a job posting url/i);
  });

  it('surfaces the server message on a non-OK response', async () => {
    global.fetch = jest
      .fn()
      .mockResolvedValue({ ok: false, status: 400 }) as unknown as typeof fetch;
    const onJobAdded = jest.fn();

    const { result } = renderHook(() => useAddJobByUrl(onJobAdded));
    await act(async () => {
      await result.current.submit({ url: 'not a url' });
    });

    expect(result.current.error).toBe('Could not add job');
    expect(onJobAdded).not.toHaveBeenCalled();
  });

  it('surfaces a network error on a thrown fetch', async () => {
    global.fetch = jest
      .fn()
      .mockRejectedValue(new Error('offline')) as unknown as typeof fetch;
    const onJobAdded = jest.fn();

    const { result } = renderHook(() => useAddJobByUrl(onJobAdded));
    await act(async () => {
      await result.current.submit({ url: 'https://x.com/job' });
    });

    expect(result.current.error).toMatch(/network error/i);
    expect(onJobAdded).not.toHaveBeenCalled();
  });

  it('keeps the modal open on failure so the typed URL survives', async () => {
    global.fetch = jest
      .fn()
      .mockResolvedValue(notAdded()) as unknown as typeof fetch;

    const { result } = renderHook(() => useAddJobByUrl(jest.fn()));
    act(() => result.current.open());
    await act(async () => {
      await result.current.submit({ url: 'https://x.com/job' });
    });

    expect(result.current.isOpen).toBe(true);
  });
});
