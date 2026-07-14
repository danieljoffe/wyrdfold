'use client';

import { useCallback, useMemo, useRef } from 'react';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';

import { JOBS_FILTER_FIELDS, type JobsFilterField } from './jobsFilterFields';
import type { JobsFilterState } from './types';

/**
 * Single source of truth for which filters / sort / page / target the
 * /jobs page is showing. Reads from ``useSearchParams`` so browser
 * back/forward "just works" — pressing back re-runs the read-from-URL
 * effects and every consumer rehydrates from the new URL automatically.
 *
 * The setters all use ``router.replace`` (not ``push``) so typing in
 * the search box doesn't generate a history entry per keystroke. The
 * tab/page setters opt into ``push`` via the ``mode`` arg so they DO
 * create history entries — that's the affordance a user expects when
 * they navigate to page 2 and then hit back.
 *
 * The filter dimensions (and their short URL keys — ``q``, ``s``,
 * ``score``, …) come from ``JOBS_FILTER_FIELDS``; only the navigation
 * keys live here.
 */

const NAV_KEYS = {
  sort: 'sort',
  order: 'order',
  target: 'target',
} as const;

type JobsUrlOrder = 'asc' | 'desc';

export interface JobsUrlState extends JobsFilterState {
  sort: string;
  order: JobsUrlOrder;
  targetId: string | undefined;
}

/** Patch shape: pass only the keys you want to change. ``null`` clears
 *  the key. Omitted keys are left untouched. */
export type JobsUrlPatch = Partial<{
  [K in keyof JobsUrlState]: JobsUrlState[K] | null;
}>;

interface UseJobsUrlStateOptions {
  /** What to set ``sort`` to when the URL doesn't specify one. */
  defaultSort: string;
  /** What to set ``order`` to when the URL doesn't specify one. */
  defaultOrder: JobsUrlOrder;
  /** Auto-pick a target tab when the URL has none. Used so a bare
   *  ``/jobs`` lands on the user's first active target rather than the
   *  empty "All Jobs" view (which the poller doesn't populate). */
  defaultTargetId?: string | undefined | null;
}

function parseOrder(raw: string | null): JobsUrlOrder {
  return raw === 'asc' ? 'asc' : 'desc';
}

export function useJobsUrlState({
  defaultSort,
  defaultOrder,
  defaultTargetId,
}: UseJobsUrlStateOptions): {
  state: JobsUrlState;
  setState: (patch: JobsUrlPatch, mode?: 'replace' | 'push') => void;
} {
  const searchParams = useSearchParams();
  const pathname = usePathname();
  const router = useRouter();

  // Snapshot the latest searchParams so the setter callback (memoised on
  // ``pathname`` / ``router``) sees up-to-date values without retriggering
  // every component that consumes the hook on every keystroke.
  const paramsRef = useRef(searchParams);
  paramsRef.current = searchParams;

  const state = useMemo<JobsUrlState>(() => {
    const target = searchParams.get(NAV_KEYS.target);
    const filters = Object.fromEntries(
      JOBS_FILTER_FIELDS.map(f => [f.field, searchParams.get(f.urlKey) ?? ''])
    ) as Record<JobsFilterField, string>;
    return {
      ...filters,
      sort: searchParams.get(NAV_KEYS.sort) ?? defaultSort,
      order: parseOrder(searchParams.get(NAV_KEYS.order)) ?? defaultOrder,
      targetId: target ?? defaultTargetId ?? undefined,
    };
  }, [searchParams, defaultSort, defaultOrder, defaultTargetId]);

  const setState = useCallback(
    (patch: JobsUrlPatch, mode: 'replace' | 'push' = 'replace') => {
      const next = new URLSearchParams(paramsRef.current.toString());
      const apply = (key: string, value: string | null | undefined) => {
        if (value === null || value === undefined || value === '') {
          next.delete(key);
        } else {
          next.set(key, value);
        }
      };
      for (const f of JOBS_FILTER_FIELDS) {
        if (f.field in patch) apply(f.urlKey, patch[f.field]);
      }
      if ('sort' in patch) apply(NAV_KEYS.sort, patch.sort);
      if ('order' in patch) apply(NAV_KEYS.order, patch.order);
      if ('targetId' in patch) apply(NAV_KEYS.target, patch.targetId);

      const qs = next.toString();
      const url = qs ? `${pathname}?${qs}` : pathname;
      if (mode === 'push') {
        router.push(url, { scroll: false });
      } else {
        router.replace(url, { scroll: false });
      }
    },
    [pathname, router]
  );

  return { state, setState };
}
