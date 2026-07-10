import { useCallback, useState } from 'react';
import { extractApiError } from '@/lib/extractApiError';
import { useToast } from '@/state/Toast/ToastProvider';

/**
 * Shared "add a job by pasting its URL" flow, used by both the jobs empty
 * state and the thin-results callout. Prompts for a URL, POSTs it to
 * `/api/jobs/manual`, toasts the outcome, and calls `onJobAdded` on success so
 * the caller can refresh its list. Owns the `submitting` flag.
 *
 * Consolidates two near-identical copies whose only real difference was error
 * handling — this keeps the richer `extractApiError` (the server's message),
 * upgrading the callout that previously hand-rolled `detail || status`.
 *
 * Uses `window.prompt` to match the codebase's existing add-job pattern; a
 * full modal isn't justified for a single input.
 */
export function useAddJobByUrl(onJobAdded: () => void) {
  const { toast } = useToast();
  const [submitting, setSubmitting] = useState(false);

  const addJobByUrl = useCallback(async () => {
    // eslint-disable-next-line no-alert -- personal tool, native prompt matches the codebase
    const url = window.prompt('Paste a job posting URL:');
    const trimmed = url?.trim() ?? '';
    if (!trimmed) return;
    setSubmitting(true);
    try {
      const res = await fetch('/api/jobs/manual', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: trimmed }),
      });
      if (!res.ok) {
        toast({
          variant: 'error',
          title: await extractApiError(res, 'Could not add job'),
        });
        return;
      }
      toast({ variant: 'success', title: 'Job added' });
      onJobAdded();
    } catch {
      toast({ variant: 'error', title: 'Network error adding job' });
    } finally {
      setSubmitting(false);
    }
  }, [onJobAdded, toast]);

  return { addJobByUrl, submitting };
}
