import { useCallback, useState } from 'react';
import { extractApiError } from '@/lib/extractApiError';
import { useToast } from '@/state/Toast/ToastProvider';

/**
 * "Remove this posting from this target" — what the Delete button should
 * always have been.
 *
 * The old flow called `DELETE /api/jobs/{id}`, which soft-archives the
 * caller's `user_jobs` row. Three things were wrong with it:
 *
 *   1. The confirm said "This can't be undone" — but `archived` is a normal
 *      status, so it was trivially reversible. Users were being scared off a
 *      safe action.
 *   2. The row stayed in the list. Both list RPCs applied no archived
 *      exclusion when no status filter was set, so a default `sort=score`
 *      view kept rendering it, just relabelled "Archived".
 *   3. The user's actual intent — get this out of my list — was never served.
 *
 * Removal is per-(user, target, job) server-side. It survives re-scoring
 * (unlike `scores.excluded`, which the scorer recomputes) and never touches
 * a co-searcher's copy of the same shared target.
 *
 * `undo` is the recourse the old flow lacked; the caller wires it into the
 * success toast.
 */
export function useJobRemove() {
  const { toast } = useToast();
  const [removing, setRemoving] = useState(false);

  const undo = useCallback(
    async (jobIds: string[]) => {
      const results = await Promise.allSettled(
        jobIds.map(id => fetch(`/api/jobs/${id}/remove`, { method: 'DELETE' }))
      );
      const restored = results.filter(
        r => r.status === 'fulfilled' && r.value.ok
      ).length;
      toast({
        variant: restored > 0 ? 'success' : 'error',
        title:
          restored > 0
            ? `Restored ${restored} ${restored === 1 ? 'job' : 'jobs'}`
            : 'Could not undo',
      });
      return restored;
    },
    [toast]
  );

  /**
   * Remove `jobIds` from `targetId` (or from every target holding them when
   * `targetId` is undefined — the All Jobs tab has no single target).
   * Resolves the number actually removed.
   */
  const removeJobs = useCallback(
    async (
      jobIds: string[],
      targetId: string | undefined,
      onUndone?: () => void
    ): Promise<number> => {
      if (jobIds.length === 0) return 0;
      setRemoving(true);
      try {
        const results = await Promise.allSettled(
          jobIds.map(id =>
            fetch(`/api/jobs/${id}/remove`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ target_id: targetId ?? null }),
            })
          )
        );
        const okIds = jobIds.filter(
          (_id, i) =>
            results[i]?.status === 'fulfilled' &&
            (results[i] as PromiseFulfilledResult<Response>).value.ok
        );
        if (okIds.length === 0) {
          const first = results.find(r => r.status === 'fulfilled');
          const detail = first
            ? await extractApiError(
                (first as PromiseFulfilledResult<Response>).value,
                'Could not remove'
              )
            : 'Network error removing jobs';
          toast({ variant: 'error', title: detail });
          return 0;
        }
        toast({
          variant: 'success',
          title: `Removed ${okIds.length} ${okIds.length === 1 ? 'job' : 'jobs'}`,
          action: {
            label: 'Undo',
            onClick: () => {
              void undo(okIds).then(() => onUndone?.());
            },
          },
        });
        return okIds.length;
      } finally {
        setRemoving(false);
      }
    },
    [toast, undo]
  );

  return { removeJobs, undo, removing };
}
