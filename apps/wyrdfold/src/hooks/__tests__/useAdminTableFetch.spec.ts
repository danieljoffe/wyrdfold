import { renderHook, waitFor, act } from '@testing-library/react';
import { useAdminTableFetch } from '../useAdminTableFetch';

const firstPage = {
  items: [
    { id: '1', name: 'Item 1' },
    { id: '2', name: 'Item 2' },
  ],
  next_cursor: 'cursor-2',
};

const secondPage = {
  items: [{ id: '3', name: 'Item 3' }],
  next_cursor: null,
};

const mockFetch = jest.fn();
const originalFetch = global.fetch;

beforeEach(() => {
  mockFetch.mockResolvedValue({
    ok: true,
    json: async () => firstPage,
  } as Response);
  global.fetch = mockFetch;
});

afterEach(() => {
  global.fetch = originalFetch;
  mockFetch.mockReset();
});

describe('useAdminTableFetch', () => {
  const defaultOptions = {
    endpoint: '/api/test',
    defaultSort: 'created_at' as const,
    dataKey: 'items',
  };

  it('fetches the first page on mount', async () => {
    const { result } = renderHook(() => useAdminTableFetch(defaultOptions));

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    expect(result.current.data).toEqual(firstPage.items);
    // A non-null next_cursor means there's another page.
    expect(result.current.hasMore).toBe(true);
    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/test?')
    );
  });

  it('sends pageSize/sort/order and no cursor on the first page', async () => {
    const { result } = renderHook(() => useAdminTableFetch(defaultOptions));

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    const url = mockFetch.mock.calls[0][0] as string;
    const params = new URLSearchParams(url.split('?')[1]);
    expect(params.get('pageSize')).toBe('20');
    expect(params.get('sort')).toBe('created_at');
    expect(params.get('order')).toBe('desc');
    expect(params.has('cursor')).toBe(false);
    expect(params.has('page')).toBe(false);
  });

  it('starts in loading state', () => {
    const { result } = renderHook(() => useAdminTableFetch(defaultOptions));
    expect(result.current.loading).toBe(true);
  });

  it('handles failed responses gracefully', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      json: async () => ({ error: 'Unauthorized' }),
    } as Response);

    const { result } = renderHook(() => useAdminTableFetch(defaultOptions));

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    expect(result.current.data).toEqual([]);
    expect(result.current.hasMore).toBe(false);
  });

  it('loadMore appends the next page and sends the cursor', async () => {
    mockFetch
      .mockResolvedValueOnce({
        ok: true,
        json: async () => firstPage,
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => secondPage,
      } as Response);

    const { result } = renderHook(() => useAdminTableFetch(defaultOptions));
    await waitFor(() => expect(result.current.loading).toBe(false));

    await act(async () => {
      await result.current.loadMore();
    });

    // Rows are appended, not replaced.
    expect(result.current.data).toEqual([
      ...firstPage.items,
      ...secondPage.items,
    ]);
    // next_cursor went null → no more pages.
    expect(result.current.hasMore).toBe(false);

    const calls = mockFetch.mock.calls;
    const lastUrl = calls[calls.length - 1][0] as string;
    const params = new URLSearchParams(lastUrl.split('?')[1]);
    expect(params.get('cursor')).toBe('cursor-2');
  });

  it('loadMore is a no-op when there is no next page', async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => secondPage, // next_cursor: null
    } as Response);

    const { result } = renderHook(() => useAdminTableFetch(defaultOptions));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.hasMore).toBe(false);

    const callsBefore = mockFetch.mock.calls.length;
    await act(async () => {
      await result.current.loadMore();
    });
    expect(mockFetch.mock.calls.length).toBe(callsBefore);
  });

  it('re-fetches the first page (resetting the list) when sort changes', async () => {
    const { result } = renderHook(() => useAdminTableFetch(defaultOptions));
    await waitFor(() => expect(result.current.loading).toBe(false));

    const callsBefore = mockFetch.mock.calls.length;
    act(() => {
      result.current.handleSort('created_at'); // flips order desc → asc
    });

    await waitFor(() => {
      expect(mockFetch.mock.calls.length).toBeGreaterThan(callsBefore);
    });
    const calls = mockFetch.mock.calls;
    const lastUrl = calls[calls.length - 1][0] as string;
    const params = new URLSearchParams(lastUrl.split('?')[1]);
    expect(params.get('order')).toBe('asc');
    expect(params.has('cursor')).toBe(false); // first page, no cursor
  });

  it('exposes refetch function', async () => {
    const { result } = renderHook(() => useAdminTableFetch(defaultOptions));

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    const callCount = mockFetch.mock.calls.length;

    await act(async () => {
      await result.current.refetch();
    });

    expect(mockFetch.mock.calls.length).toBe(callCount + 1);
  });
});
