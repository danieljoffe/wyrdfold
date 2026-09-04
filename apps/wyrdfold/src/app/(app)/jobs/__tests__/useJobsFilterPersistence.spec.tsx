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
      if (init?.method === 'PATCH') {
        return Promise.resolve({ ok: true, json: async () => ({}) });
      }
      if (!ok) return Promise.reject(new Error('network'));
      return Promise.resolve({ ok: true, json: async () => ({ filters }) });
    });
  global.fetch = fetchMock as unknown as typeof fetch;
}

function lastPatch(): {
  body: { filters: Record<string, unknown> };
  init: RequestInit;
} | null {
  const call = [...fetchMock.mock.calls]
    .reverse()
    .find(([, init]) => (init as RequestInit | undefined)?.method === 'PATCH');
  if (!call) return null;
  const init = call[1] as RequestInit;
  return { body: JSON.parse(init.body as string), init };
}

function lastPatchBody(): { filters: Record<string, unknown> } | null {
  return lastPatch()?.body ?? null;
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

  it('write debounces one PATCH carrying only the changed keys', async () => {
    mockServer({});
    const { result } = renderHook(() => useJobsFilterPersistence());
    await waitFor(() => expect(result.current.ready).toBe(true));

    act(() => {
      result.current.write('target-1', POPULATED);
      result.current.write(undefined, { ...POPULATED, search: 'all' });
    });
    expect(lastPatchBody()).toBeNull(); // still inside the debounce window
    act(() => {
      jest.advanceTimersByTime(700);
    });

    const body = lastPatchBody();
    expect(body).not.toBeNull();
    expect(Object.keys(body!.filters).sort()).toEqual(['__all__', 'target-1']);
  });

  it('a write BEFORE hydration completes still persists — the e2e re-entry class', async () => {
    // The whole-map-PUT design made every write wait on the hydrate GET,
    // and authed-filters-persist.spec.ts proved a fast navigation outran
    // it. A per-key patch is safe to send from the first render.
    let resolveGet!: (v: unknown) => void;
    fetchMock = jest
      .fn()
      .mockImplementation((_url: string, init?: RequestInit) => {
        if (init?.method === 'PATCH') {
          return Promise.resolve({ ok: true, json: async () => ({}) });
        }
        return new Promise(res => {
          resolveGet = res; // hydrate GET intentionally left hanging
        });
      });
    global.fetch = fetchMock as unknown as typeof fetch;

    const { result } = renderHook(() => useJobsFilterPersistence());
    expect(result.current.ready).toBe(false);

    act(() => {
      result.current.write('target-1', POPULATED);
      jest.advanceTimersByTime(400);
    });

    const sent = lastPatchBody();
    expect(sent).not.toBeNull();
    expect(Object.keys(sent!.filters)).toEqual(['target-1']);

    // Hydration lands afterwards: the pending write must win the overlay.
    await act(async () => {
      resolveGet({
        ok: true,
        json: async () => ({
          filters: { 'target-1': { ...POPULATED, search: 'stale-server' } },
        }),
      });
    });
    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.read('target-1')).toEqual(POPULATED);
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
    expect(lastPatchBody()!.filters).toEqual({ 'target-1': null });
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
    expect(lastPatchBody()!.filters).toEqual({ 'target-1': null });
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
    expect(lastPatchBody()).toBeNull(); // debounce pending
    unmount();

    const body = lastPatchBody();
    expect(body).not.toBeNull();
    expect(Object.keys(body!.filters)).toEqual(['target-1']);
    expect(lastPatch()!.init.keepalive).toBe(true);
  });

  it('pagehide flushes a pending write — hard navigations persist too', async () => {
    mockServer({});
    const { result } = renderHook(() => useJobsFilterPersistence());
    await waitFor(() => expect(result.current.ready).toBe(true));

    act(() => {
      result.current.write('target-1', POPULATED);
      window.dispatchEvent(new Event('pagehide'));
    });

    expect(lastPatchBody()).not.toBeNull();
  });

  it('a clean exit sends nothing', async () => {
    mockServer({ 'target-1': POPULATED });
    const { result, unmount } = renderHook(() => useJobsFilterPersistence());
    await waitFor(() => expect(result.current.ready).toBe(true));
    unmount();
    expect(lastPatchBody()).toBeNull();
  });
});
