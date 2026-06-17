import { useCallback, useEffect, useState } from 'react';
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
    setLoading(true);
    try {
      const res = await fetch(buildUrl(null));
      if (!res.ok) return;
      const json = (await res.json()) as ListResponse;
      setData((json[dataKey] as T[] | undefined) ?? []);
      setNextCursor(json.next_cursor ?? null);
    } finally {
      setLoading(false);
    }
  }, [buildUrl, dataKey]);

  // First page — re-runs (resetting the accumulated list) whenever sort,
  // order, or filters change, because ``buildUrl`` is keyed on all three.
  useEffect(() => {
    fetchFirstPage();
  }, [fetchFirstPage]);

  const loadMore = useCallback(async () => {
    if (!nextCursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const res = await fetch(buildUrl(nextCursor));
      if (!res.ok) return;
      const json = (await res.json()) as ListResponse;
      const rows = (json[dataKey] as T[] | undefined) ?? [];
      setData(prev => [...prev, ...rows]);
      setNextCursor(json.next_cursor ?? null);
    } finally {
      setLoadingMore(false);
    }
  }, [buildUrl, dataKey, nextCursor, loadingMore]);

  return {
    data,
    loading,
    loadingMore,
    hasMore: nextCursor !== null,
    loadMore,
    sort,
    order,
    handleSort,
    sortIndicator,
    refetch: fetchFirstPage,
  } as const;
}
