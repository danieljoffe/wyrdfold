'use client';

/**
 * Per-target /jobs filter persistence — SERVER-SIDE since #866.
 *
 * The original localStorage layer used a global key, so on a shared browser
 * a new account inherited the previous user's filters and opened /jobs on
 * "No jobs found" despite having matches. The page deliberately exposes no
 * user id to scope client storage by, so the snapshots now live on the
 * caller's own `user_profiles` row (owner's call on #866: server-side
 * prefs over auth-change clearing or a hashed-id storage key) — scoped to
 * the ACCOUNT by RLS, synced across devices for free.
 *
 * Shape is unchanged: a map of ``{targetId | "__all__": JobsFilterState}``.
 * The hook loads the whole map once (`ready` flips when it lands), serves
 * reads from memory, and write-through-debounces the full map back via
 * ``PUT /api/profile/jobs-filters``. Sort/order/page/targetId are still
 * NOT persisted — navigation state, not filter state.
 *
 * Failure posture: if the load fails, the page runs with session-only
 * in-memory persistence (works, just forgets on reload) — same "loses the
 * convenience, never the page" contract the localStorage layer had. Writes
 * are debounced ~600ms; a navigation inside that window can lose the last
 * snapshot, which is the same class of best-effort the old layer accepted.
 *
 * Legacy cleanup: on a successful first load the old global
 * ``wyrdfold.filters.*`` keys are deleted. They are deliberately NOT
 * imported — on a shared browser those keys belong to whoever wrote them,
 * and importing would persist the very cross-account leak this fixes.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { coerceStoredFilters, isFilterStateEmpty } from './jobsFilterFields';
import type { JobsFilterState } from './types';

const LEGACY_STORAGE_PREFIX = 'wyrdfold.filters.';
const ALL_JOBS_KEY = '__all__';
const WRITE_DEBOUNCE_MS = 600;

function mapKey(targetId: string | undefined): string {
  return targetId ?? ALL_JOBS_KEY;
}

function dropLegacyLocalStorageKeys(): void {
  try {
    const doomed: string[] = [];
    for (let i = 0; i < window.localStorage.length; i++) {
      const k = window.localStorage.key(i);
      if (k && k.startsWith(LEGACY_STORAGE_PREFIX)) doomed.push(k);
    }
    doomed.forEach(k => window.localStorage.removeItem(k));
  } catch {
    // Storage disabled — nothing to clean.
  }
}

interface JobsFilterPersistence {
  /** False until the server map has loaded (or the load failed and the
   *  session-only fallback engaged). Callers gate restore-on-entry on it. */
  ready: boolean;
  read: (targetId: string | undefined) => JobsFilterState | null;
  write: (targetId: string | undefined, filters: JobsFilterState) => void;
  clear: (targetId: string | undefined) => void;
}

export function useJobsFilterPersistence(): JobsFilterPersistence {
  const [ready, setReady] = useState(false);
  const mapRef = useRef<Record<string, JobsFilterState>>({});
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch('/api/profile/jobs-filters')
      .then(res => (res.ok ? res.json() : null))
      .then((data: { filters?: Record<string, unknown> } | null) => {
        if (cancelled) return;
        if (data && data.filters && typeof data.filters === 'object') {
          const coerced: Record<string, JobsFilterState> = {};
          for (const [key, value] of Object.entries(data.filters)) {
            const filters = coerceStoredFilters(value);
            if (filters && !isFilterStateEmpty(filters)) {
              coerced[key] = filters;
            }
          }
          mapRef.current = coerced;
          dropLegacyLocalStorageKeys();
        }
        setReady(true);
      })
      .catch(() => {
        // Session-only fallback: reads/writes work in memory for this
        // mount; nothing persists. The page must not block on prefs.
        if (!cancelled) setReady(true);
      });
    return () => {
      cancelled = true;
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  const scheduleFlush = useCallback((): void => {
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      fetch('/api/profile/jobs-filters', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filters: mapRef.current }),
      }).catch(() => {
        // Best-effort — the in-memory copy stays authoritative for this
        // session; the next successful flush carries the full map anyway.
      });
    }, WRITE_DEBOUNCE_MS);
  }, []);

  const read = useCallback(
    (targetId: string | undefined): JobsFilterState | null =>
      mapRef.current[mapKey(targetId)] ?? null,
    []
  );

  const write = useCallback(
    (targetId: string | undefined, filters: JobsFilterState): void => {
      // Drop the entry entirely when all fields are empty so "clear all
      // filters" doesn't leave a stale snapshot that re-applies next visit.
      if (isFilterStateEmpty(filters)) {
        if (!(mapKey(targetId) in mapRef.current)) return;
        delete mapRef.current[mapKey(targetId)];
      } else {
        mapRef.current = { ...mapRef.current, [mapKey(targetId)]: filters };
      }
      scheduleFlush();
    },
    [scheduleFlush]
  );

  const clear = useCallback(
    (targetId: string | undefined): void => {
      if (!(mapKey(targetId) in mapRef.current)) return;
      delete mapRef.current[mapKey(targetId)];
      scheduleFlush();
    },
    [scheduleFlush]
  );

  return { ready, read, write, clear };
}
