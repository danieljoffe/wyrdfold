import { useCallback, useRef, useState } from 'react';

import { extractApiError } from '@/lib/extractApiError';

/** Minimal projection of a user's target for the "Add to target" picker. */
export interface TargetOption {
  id: string;
  label: string;
}

/**
 * Shared (fetch-once) state for the user's targets, threaded to every "Add to
 * target" picker so N pickers don't each refetch `/api/targets/mine`. Built by
 * {@link useTargetsSource}; consumed by `AddToTargetMenu` (and through it the
 * listing detail, #467 §11).
 */
export interface TargetsSource {
  targets: TargetOption[] | null;
  loading: boolean;
  error: string | null;
  ensureLoaded: () => void;
}

/**
 * The user's targets for the "Add to target" picker — fetched ONCE, lazily,
 * the first time any menu opens (`ensureLoaded`). Extracted from
 * JobSearchExplorer when the listing detail became URL-addressable (#467 §11.2
 * fast-follow): the detail routes (intercepted modal + full page) now host the
 * picker outside the explorer, and all hosts must share this exact
 * fetch-once/retry-on-failure behavior. `requested` stops multiple menus from
 * each kicking a fetch; it resets on failure so a later open retries.
 */
export function useTargetsSource(): TargetsSource {
  const [targets, setTargets] = useState<TargetOption[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requested = useRef(false);

  const ensureLoaded = useCallback(() => {
    if (requested.current) return;
    requested.current = true;
    setLoading(true);
    setError(null);
    void (async () => {
      try {
        const res = await fetch('/api/targets/mine');
        if (!res.ok) {
          throw new Error(await extractApiError(res, 'Failed to load targets'));
        }
        const data = (await res.json()) as {
          targets: { target: { id: string; label: string } }[];
        };
        setTargets(
          data.targets.map(t => ({ id: t.target.id, label: t.target.label }))
        );
      } catch (e) {
        requested.current = false; // allow a retry on the next open
        setError(e instanceof Error ? e.message : 'Failed to load targets.');
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  return { targets, loading, error, ensureLoaded };
}
