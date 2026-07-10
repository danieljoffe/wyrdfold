import { useCallback, useState } from 'react';
import { extractApiError } from '@/lib/extractApiError';
import { useToast } from '@/state/Toast/ToastProvider';

/**
 * Shared job-deletion for every jobs surface (inline detail panel, full
 * detail page, mobile list, and the bulk action bar). Owns the
 * `DELETE /api/jobs/{id}` call, the in-flight `deleting` flag, and the
 * success/error toasts — so the four call sites stop re-implementing (and
 * drifting on) the same fetch. Callers keep their own post-delete behaviour
 * (navigate away, refetch, clear selection) by acting on the returned
 * boolean / count.
 *
 * Error copy is the richest of the former per-site variants: the server's
 * message via `extractApiError` on a non-OK response, and a distinct
 * "Network error deleting job" on a thrown fetch — an upgrade for the three
 * sites that previously showed a flat "Failed to delete job" for both.
 */
export function useJobDelete() {
  const { toast } = useToast();
  const [deleting, setDeleting] = useState(false);

  /** Delete one job. Resolves `true` on success. Toasts on failure. */
  const deleteJob = useCallback(
    async (jobId: string): Promise<boolean> => {
      setDeleting(true);
      try {
        const res = await fetch(`/api/jobs/${jobId}`, { method: 'DELETE' });
        if (res.ok) {
          toast({ variant: 'success', title: 'Job deleted' });
          return true;
        }
        toast({
          variant: 'error',
          title: await extractApiError(res, 'Failed to delete job'),
        });
        return false;
      } catch {
        toast({ variant: 'error', title: 'Network error deleting job' });
        return false;
      } finally {
        setDeleting(false);
      }
    },
    [toast]
  );

  /**
   * Delete many jobs concurrently. Resolves the number actually deleted and
   * toasts a summary. Partial failures still report the successes (mirrors the
   * bulk action bar's original best-effort semantics).
   */
  const deleteJobs = useCallback(
    async (jobIds: string[]): Promise<number> => {
      if (jobIds.length === 0) return 0;
      setDeleting(true);
      try {
        const results = await Promise.allSettled(
          jobIds.map(id => fetch(`/api/jobs/${id}`, { method: 'DELETE' }))
        );
        const deleted = results.filter(
          r => r.status === 'fulfilled' && r.value.ok
        ).length;
        toast({
          variant: deleted > 0 ? 'success' : 'error',
          title:
            deleted > 0 ? `Deleted ${deleted} jobs` : 'Failed to delete jobs',
        });
        return deleted;
      } finally {
        setDeleting(false);
      }
    },
    [toast]
  );

  return { deleteJob, deleteJobs, deleting };
}
