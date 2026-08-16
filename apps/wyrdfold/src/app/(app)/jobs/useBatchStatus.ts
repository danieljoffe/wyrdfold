import { useCallback, useState } from 'react';
import { useToast } from '@/state/Toast/ToastProvider';
import type { JobStatus } from './types';
import { formatStatus } from './types';

/**
 * Set one status across several selected postings (#10).
 *
 * Status was the only pipeline action with no bulk form: marking ten jobs
 * "applied" after an application session meant opening each one, changing the
 * dropdown, and going back — ten round trips through the detail panel for a
 * change the list already had selected.
 *
 * There is no bulk status endpoint, and this deliberately doesn't add one:
 * `POST /jobs/{id}/status` writes a `status_log` entry per posting, so the
 * per-job history stays intact and a partial failure leaves the successes
 * applied. `BATCH_MAX` caps selection at 20, so the fan-out is bounded.
 *
 * Mirrors `useJobRemove` — `Promise.allSettled`, count the ones that actually
 * landed, and report the partial honestly rather than claiming a clean sweep.
 */
export function useBatchStatus() {
  const { toast } = useToast();
  const [updating, setUpdating] = useState(false);

  const setStatusForJobs = useCallback(
    async (jobIds: string[], status: JobStatus): Promise<number> => {
      if (jobIds.length === 0) return 0;
      setUpdating(true);
      try {
        const results = await Promise.allSettled(
          jobIds.map(id =>
            fetch(`/api/jobs/${id}/status`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ status }),
            })
          )
        );
        const ok = results.filter(
          r => r.status === 'fulfilled' && r.value.ok
        ).length;
        const failed = jobIds.length - ok;

        if (ok === 0) {
          toast({
            variant: 'error',
            title: `Could not update ${jobIds.length === 1 ? 'that job' : 'those jobs'}`,
          });
        } else {
          toast({
            variant: failed > 0 ? 'warning' : 'success',
            title: `${ok} ${ok === 1 ? 'job' : 'jobs'} marked ${formatStatus(status)}`,
            // Naming the leftover matters: a silent partial looks identical to
            // a full success, and the user would never retry the stragglers.
            ...(failed > 0
              ? { description: `${failed} could not be updated — try again.` }
              : {}),
          });
        }
        return ok;
      } finally {
        setUpdating(false);
      }
    },
    [toast]
  );

  return { setStatusForJobs, updating };
}
