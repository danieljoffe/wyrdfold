import { act, renderHook, waitFor } from '@testing-library/react';

import { emptyFilters } from '../jobsFilterFields';
import type { JobsFilterState } from '../types';
import { useJobsFilterPersistence } from '../useJobsFilterPersistence';

// #866: persistence moved server-side — the localStorage layer's global key
// leaked one account's filters into the next on shared browsers. The hook
// now hydrates once from /api/profile/jobs-filters, serves reads from
// memory, and write-through-debounces the whole map back.

const POPULATED: JobsFilterState = {
  ...emptyFilters(),
  search: 'react',
  status: 'new',
  minScore: '60',
  excludeLocations: 'UK',
  onlyLocations: 'US',
  remoteOnly: 'true',
  minSalary: '150000',
  country: 'US',
};

const originalFetch = global.fetch;
let fetchMock: jest.Mock;

function mockServer(filters: Record<string, unknown> | null, ok = true) {
  fetchMock = jest
    .fn()
    .mockImplementation((_url: string, init?: RequestInit) => {
      if (init?.method === 'PUT') {
        return Promise.resolve({ ok: true, json: async () => ({}) });
      }
      if (!ok) return Promise.reject(new Error('network'));
      return Promise.resolve({ ok: true, json: async () => ({ filters }) });
    });
  global.fetch = fetchMock as unknown as typeof fetch;
}

function lastPutBody(): { filters: Record<string, unknown> } | null {
  const put = [...fetchMock.mock.calls]
    .reverse()
    .find(([, init]) => (init as RequestInit | undefined)?.method === 'PUT');
  if (!put) return null;
  return JSON.parse((put[1] as RequestInit).body as string);
}

beforeEach(() => {
  jest.useFakeTimers();
  window.localStorage.clear();
});

afterEach(() => {
  jest.runOnlyPendingTimers();
  jest.useRealTimers();
  global.fetch = originalFetch;
  jest.clearAllMocks();
});

describe('useJobsFilterPersistence (server-backed, #866)', () => {
  it('hydrates from the server and serves reads per key, __all__ for undefined', async () => {
    mockServer({
      'target-1': POPULATED,
      __all__: { ...POPULATED, search: 'all' },
    });
    const { result } = renderHook(() => useJobsFilterPersistence());
    await waitFor(() => expect(result.current.ready).toBe(true));

    expect(result.current.read('target-1')).toEqual(POPULATED);
    expect(result.current.read(undefined)?.search).toBe('all');
    expect(result.current.read('target-unknown')).toBeNull();
  });

  it('is not ready before the server map lands — restore must wait', () => {
    mockServer({ 'target-1': POPULATED });
    const { result } = renderHook(() => useJobsFilterPersistence());
    expect(result.current.ready).toBe(false);
    expect(result.current.read('target-1')).toBeNull();
  });

  it('drops malformed server entries instead of serving them', async () => {
    mockServer({ 'target-1': 'not-an-object', 'target-2': POPULATED });
    const { result } = renderHook(() => useJobsFilterPersistence());
    await waitFor(() => expect(result.current.ready).toBe(true));

    expect(result.current.read('target-1')).toBeNull();
    expect(result.current.read('target-2')).toEqual(POPULATED);
  });

  it('write debounces one PUT carrying the whole map', async () => {
    mockServer({});
    const { result } = renderHook(() => useJobsFilterPersistence());
    await waitFor(() => expect(result.current.ready).toBe(true));

    act(() => {
      result.current.write('target-1', POPULATED);
      result.current.write(undefined, { ...POPULATED, search: 'all' });
    });
    expect(lastPutBody()).toBeNull(); // still inside the debounce window
    act(() => {
      jest.advanceTimersByTime(700);
    });

    const body = lastPutBody();
    expect(body).not.toBeNull();
    expect(Object.keys(body!.filters).sort()).toEqual(['__all__', 'target-1']);
  });

  it('an all-empty write deletes the snapshot so cleared filters stay cleared', async () => {
    mockServer({ 'target-1': POPULATED });
    const { result } = renderHook(() => useJobsFilterPersistence());
    await waitFor(() => expect(result.current.ready).toBe(true));

    act(() => {
      result.current.write('target-1', emptyFilters());
      jest.advanceTimersByTime(700);
    });

    expect(result.current.read('target-1')).toBeNull();
    expect(lastPutBody()!.filters).toEqual({});
  });

  it('clear removes the entry and flushes', async () => {
    mockServer({ 'target-1': POPULATED });
    const { result } = renderHook(() => useJobsFilterPersistence());
    await waitFor(() => expect(result.current.ready).toBe(true));

    act(() => {
      result.current.clear('target-1');
      jest.advanceTimersByTime(700);
    });

    expect(result.current.read('target-1')).toBeNull();
    expect(lastPutBody()!.filters).toEqual({});
  });

  it('deletes the legacy global localStorage keys and does NOT import them', async () => {
    // On a shared browser those keys belong to whoever wrote them —
    // importing would persist the exact cross-account leak #866 fixes.
    window.localStorage.setItem(
      'wyrdfold.filters.__all__',
      JSON.stringify(POPULATED)
    );
    mockServer({});
    const { result } = renderHook(() => useJobsFilterPersistence());
    await waitFor(() => expect(result.current.ready).toBe(true));

    expect(window.localStorage.getItem('wyrdfold.filters.__all__')).toBeNull();
    expect(result.current.read(undefined)).toBeNull();
  });

  it('load failure degrades to session-only memory, never blocks the page', async () => {
    mockServer(null, false);
    const { result } = renderHook(() => useJobsFilterPersistence());
    await waitFor(() => expect(result.current.ready).toBe(true));

    act(() => {
      result.current.write('target-1', POPULATED);
    });
    expect(result.current.read('target-1')).toEqual(POPULATED);
  });
});

describe('flush-on-exit (#866 — the e2e re-entry regression)', () => {
  // The first cut only debounced; the timer died with the page and the
  // snapshot was silently lost — caught by authed-filters-persist.spec.ts.
  it('an unmount with a pending write SENDS it (keepalive), never drops it', async () => {
    mockServer({});
    const { result, unmount } = renderHook(() => useJobsFilterPersistence());
    await waitFor(() => expect(result.current.ready).toBe(true));

    act(() => {
      result.current.write('target-1', POPULATED);
    });
    expect(lastPutBody()).toBeNull(); // debounce pending
    unmount();

    const body = lastPutBody();
    expect(body).not.toBeNull();
    expect(Object.keys(body!.filters)).toEqual(['target-1']);
    const putInit = [...fetchMock.mock.calls]
      .reverse()
      .find(
        ([, init]) => (init as RequestInit | undefined)?.method === 'PUT'
      )![1] as RequestInit;
    expect(putInit.keepalive).toBe(true);
  });

  it('pagehide flushes a pending write — hard navigations persist too', async () => {
    mockServer({});
    const { result } = renderHook(() => useJobsFilterPersistence());
    await waitFor(() => expect(result.current.ready).toBe(true));

    act(() => {
      result.current.write('target-1', POPULATED);
      window.dispatchEvent(new Event('pagehide'));
    });

    expect(lastPutBody()).not.toBeNull();
  });

  it('a clean exit sends nothing', async () => {
    mockServer({ 'target-1': POPULATED });
    const { result, unmount } = renderHook(() => useJobsFilterPersistence());
    await waitFor(() => expect(result.current.ready).toBe(true));
    unmount();
    expect(lastPutBody()).toBeNull();
  });
});
