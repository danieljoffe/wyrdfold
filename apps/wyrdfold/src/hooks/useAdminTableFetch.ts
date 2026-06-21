import { useCallback, useEffect, useRef, useState } from 'react';
import { useTableSort } from './useTableSort';

interface UseAdminTableFetchOptions<S extends string> {
  /** API endpoint path (e.g. '/api/jobs') */
  endpoint: string;
  /** Default sort column */
  defaultSort: S;
  /** Default sort order */
  defaultOrder?: 'asc' | 'desc';
  /** Number of items to request per page */
  pageSize?: number;
  /** Key in the API response that holds the data array (e.g. 'postings') */
  dataKey: string;
  /** Additional query params to include (e.g. filters) */
  extraParams?: Record<string, string>;
  /** External source of truth for sort/order (e.g. URL query params).
   *  When provided, the hook re-initialises its sort state any time the
   *  controlled value changes, so browser back/forward restores it.
   *  Pagination is NOT in the URL — it's an in-memory cursor (see below). */
  controlled?:
    | {
        sort: S;
        order: 'asc' | 'desc';
      }
    | undefined;
  /** Fired whenever the user sorts. Lets the caller mirror the change
   *  into URL state so the back button restores it. */
  onSortChange?: ((sort: S, order: 'asc' | 'desc') => void) | undefined;
}

interface ListResponse {
  next_cursor?: string | null;
  [key: string]: unknown;
}

/**
 * Cursor ("load more") list fetch. The list view appends pages rather than
 * jumping to numbered pages: keyset pagination on the API side can't do random
 * page access or return an exact total, so the contract is an opaque
 * ``next_cursor`` (null = last page). Sort/order/filters live in the URL; the
 * accumulated rows + cursor are in-memory and reset to the first page whenever
 * sort, order, or filters change.
 *
 * Concurrency: every fetch carries a monotonic request id + an ``AbortController``
 * (mirrors {@link useInsights}). Rapidly changing the filter/sort fires
 * overlapping ``/api/...`` requests; the id check drops any response that is no
 * longer the latest, so a slow stale response can't clobber the current result.
 */
export function useAdminTableFetch<T, S extends string>({
  endpoint,
  defaultSort,
  defaultOrder,
  pageSize = 20,
  dataKey,
  extraParams,
  controlled,
  onSortChange,
}: UseAdminTableFetchOptions<S>) {
  const [data, setData] = useState<T[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | undefined>(undefined);

  // Monotonic request id + the in-flight controller, shared by the first-page
  // fetch and load-more. A filter/sort change re-runs the first-page fetch;
  // without this a slower *stale* response could resolve last and clobber the
  // current result, or an in-flight load-more could append rows for the old
  // filter.
  const requestRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);

  const { sort, order, handleSort, sortIndicator } = useTableSort<S>(
    defaultSort,
    (nextSort, nextOrder) => {
      onSortChange?.(nextSort, nextOrder);
    },
    defaultOrder,
    controlled ? { sort: controlled.sort, order: controlled.order } : undefined
  );

  const buildUrl = useCallback(
    (cursor: string | null) => {
      const params = new URLSearchParams({
        pageSize: String(pageSize),
        sort,
        order,
        ...extraParams,
      });
      if (cursor) params.set('cursor', cursor);
      return `${endpoint}?${params}`;
    },
    [endpoint, pageSize, sort, order, extraParams]
  );

  const fetchFirstPage = useCallback(async () => {
    // Cancel any in-flight request and claim a new sequence id, so a slower
    // stale response (from a prior filter/sort) can't resolve last and clobber
    // this one.
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const requestId = ++requestRef.current;

    setLoading(true);
    try {
      const res = await fetch(buildUrl(null), { signal: controller.signal });
      if (requestRef.current !== requestId) return; // superseded
      if (!res.ok) {
        setError('Failed to load. Please try again.');
        return;
      }
      const json = (await res.json()) as ListResponse;
      if (requestRef.current !== requestId) return; // superseded during parse
      setData((json[dataKey] as T[] | undefined) ?? []);
      setNextCursor(json.next_cursor ?? null);
      setError(undefined);
    } catch (err) {
      if (controller.signal.aborted) return;
      if (requestRef.current !== requestId) return;
      if (err instanceof Error && err.name === 'AbortError') return;
      setError('Failed to load. Please try again.');
    } finally {
      // Only the current request owns the shared ``loading`` flag — a
      // superseded one must not flip it off while the latest is still running.
      if (requestRef.current === requestId) setLoading(false);
    }
  }, [buildUrl, dataKey]);

  // First page — re-runs (resetting the accumulated list) whenever sort,
  // order, or filters change, because ``buildUrl`` is keyed on all three.
  // Cleanup aborts the in-flight request on unmount / before the next run.
  useEffect(() => {
    fetchFirstPage();
    return () => abortRef.current?.abort();
  }, [fetchFirstPage]);

  const loadMore = useCallback(async () => {
    if (!nextCursor || loadingMore) return;
    const controller = new AbortController();
    abortRef.current = controller;
    // Continue the *current* list — don't bump the sequence. If a first-page
    // fetch supersedes us mid-flight (filter changed), the id check below drops
    // these now-stale appended rows.
    const requestId = requestRef.current;
    setLoadingMore(true);
    try {
      const res = await fetch(buildUrl(nextCursor), {
        signal: controller.signal,
      });
      if (requestRef.current !== requestId) return; // superseded by a new first page
      if (!res.ok) {
        setError('Failed to load more. Please try again.');
        return;
      }
      const json = (await res.json()) as ListResponse;
      if (requestRef.current !== requestId) return;
      const rows = (json[dataKey] as T[] | undefined) ?? [];
      setData(prev => [...prev, ...rows]);
      setNextCursor(json.next_cursor ?? null);
    } catch (err) {
      if (controller.signal.aborted) return;
      if (requestRef.current !== requestId) return;
      if (err instanceof Error && err.name === 'AbortError') return;
      setError('Failed to load more. Please try again.');
    } finally {
      setLoadingMore(false);
    }
  }, [buildUrl, dataKey, nextCursor, loadingMore]);

  return {
    data,
    loading,
    loadingMore,
    error,
    hasMore: nextCursor !== null,
    loadMore,
    sort,
    order,
    handleSort,
    sortIndicator,
    refetch: fetchFirstPage,
  } as const;
}
