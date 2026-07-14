'use client';

/**
 * Per-target filter persistence in localStorage.
 *
 * The /jobs page used to lose filter state any time the user navigated
 * away and returned via a link that didn't preserve the query string
 * (e.g. the sidebar's /jobs link, the dashboard's "view all" links).
 * URL was the SoT but the URL wasn't always carried back in.
 *
 * This hook layers localStorage on top: every time the URL filter
 * state changes, snapshot it into ``localStorage[wyrdfold.filters.<key>]``.
 * On entry — if the URL is bare AND a snapshot exists for the current
 * target — restore the snapshot into the URL via the caller's setter.
 *
 * Keyed per target so each target remembers its own filters. The All
 * Jobs view uses the ``__all__`` sentinel. The persisted payload is the
 * full ``JobsFilterState`` — every dimension in ``JOBS_FILTER_FIELDS``
 * (a v1 payload holding only the original five fields still restores
 * those five; see ``coerceStoredFilters``). Sort/order/page/targetId are
 * NOT persisted — those are navigation state, not filter state.
 *
 * Failures (SSR, quota exceeded, disabled storage) are silent: read
 * returns ``null``, write becomes a no-op. The page works without
 * persistence; it just loses the convenience.
 */

import { useCallback } from 'react';

import { coerceStoredFilters, isFilterStateEmpty } from './jobsFilterFields';
import type { JobsFilterState } from './types';

const STORAGE_PREFIX = 'wyrdfold.filters.';
const ALL_JOBS_KEY = '__all__';

function storageKey(targetId: string | undefined): string {
  return `${STORAGE_PREFIX}${targetId ?? ALL_JOBS_KEY}`;
}

interface JobsFilterPersistence {
  read: (targetId: string | undefined) => JobsFilterState | null;
  write: (targetId: string | undefined, filters: JobsFilterState) => void;
  clear: (targetId: string | undefined) => void;
}

export function useJobsFilterPersistence(): JobsFilterPersistence {
  const read = useCallback(
    (targetId: string | undefined): JobsFilterState | null => {
      if (typeof window === 'undefined') return null;
      try {
        const raw = window.localStorage.getItem(storageKey(targetId));
        if (!raw) return null;
        return coerceStoredFilters(JSON.parse(raw));
      } catch {
        // Malformed JSON, parser error, etc. — treat as missing.
        return null;
      }
    },
    []
  );

  const write = useCallback(
    (targetId: string | undefined, filters: JobsFilterState): void => {
      if (typeof window === 'undefined') return;
      try {
        // Drop the entry entirely when all fields are empty so a "clear
        // all filters" action doesn't leave a stale snapshot that would
        // re-apply on the next visit.
        if (isFilterStateEmpty(filters)) {
          window.localStorage.removeItem(storageKey(targetId));
          return;
        }
        window.localStorage.setItem(
          storageKey(targetId),
          JSON.stringify(filters)
        );
      } catch {
        // Quota exceeded / storage disabled — silent.
      }
    },
    []
  );

  const clear = useCallback((targetId: string | undefined): void => {
    if (typeof window === 'undefined') return;
    try {
      window.localStorage.removeItem(storageKey(targetId));
    } catch {
      // ignore
    }
  }, []);

  return { read, write, clear };
}
