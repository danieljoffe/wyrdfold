import { useCallback, useState } from 'react';
import { extractApiError } from '@/lib/extractApiError';
import { useToast } from '@/state/Toast/ToastProvider';
import { describeAddJobFailure } from './addJobFailure';

/**
 * Shared "add a job by pasting its URL" flow, used by the jobs empty state,
 * the thin-results callout, and the list toolbar.
 *
 * Two things this hook has to get right, both of which it previously got
 * wrong:
 *
 * 1. **`res.ok` is not a success check here.** `POST /jobs/manual` answers
 *    *200* with `{success: false, needs_manual_fields: true}` whenever it
 *    fetched a page but couldn't read a posting out of it — only a malformed
 *    URL earns a 4xx. Branching on `res.ok` alone meant every unreadable URL
 *    (LinkedIn 403s the extractor) toasted a green "Job added" for a job that
 *    was never created. We branch on `body.success`.
 *
 * 2. **The failure has to survive long enough to act on.** The old flow
 *    collected the URL through `window.prompt`, so there was nowhere to put an
 *    error except a toast that outlived the input by nothing. State lives here
 *    and the modal renders `error` next to the field, keeping the typed URL —
 *    the same pattern `CreateTargetModal` uses for its from-URL failures.
 *
 * When the API reports `needs_manual_fields` it also returns whatever it *did*
 * extract; the caller pre-fills those into a title/company/location form and
 * re-submits, which the endpoint already supports (user values win over
 * extraction).
 */

export interface ExtractedFields {
  title: string | null;
  company_name: string | null;
  location: string | null;
}

export interface AddJobSubmission {
  url: string;
  title?: string;
  company_name?: string;
  location?: string;
}

export function useAddJobByUrl(onJobAdded: () => void) {
  const { toast } = useToast();
  const [isOpen, setIsOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [needsManualFields, setNeedsManualFields] = useState(false);
  const [extracted, setExtracted] = useState<ExtractedFields | null>(null);

  const reset = useCallback(() => {
    setError(null);
    setNeedsManualFields(false);
    setExtracted(null);
  }, []);

  const open = useCallback(() => {
    reset();
    setIsOpen(true);
  }, [reset]);

  const close = useCallback(() => {
    setIsOpen(false);
    reset();
  }, [reset]);

  /** Resolves true when a posting was actually created. */
  const submit = useCallback(
    async (input: AddJobSubmission): Promise<boolean> => {
      const url = input.url.trim();
      if (!url) {
        setError('Paste a job posting URL.');
        return false;
      }
      setSubmitting(true);
      setError(null);
      try {
        const res = await fetch('/api/jobs/manual', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            url,
            ...(input.title ? { title: input.title } : {}),
            ...(input.company_name ? { company_name: input.company_name } : {}),
            ...(input.location ? { location: input.location } : {}),
          }),
        });

        // Transport / validation failures (malformed URL, SSRF reject, 502).
        if (!res.ok) {
          setError(await extractApiError(res, 'Could not add job'));
          return false;
        }

        const body = (await res.json()) as {
          success?: boolean;
          posting_id?: string | null;
          extracted?: ExtractedFields;
          needs_manual_fields?: boolean;
          warnings?: string[];
        };

        // 200 but nothing was created — the case that used to toast success.
        if (!body.success || !body.posting_id) {
          setError(describeAddJobFailure(body.warnings ?? []));
          setNeedsManualFields(Boolean(body.needs_manual_fields));
          setExtracted(body.extracted ?? null);
          return false;
        }

        toast({ variant: 'success', title: 'Job added' });
        setIsOpen(false);
        reset();
        onJobAdded();
        return true;
      } catch {
        setError('Network error adding job.');
        return false;
      } finally {
        setSubmitting(false);
      }
    },
    [onJobAdded, reset, toast]
  );

  return {
    isOpen,
    open,
    close,
    submit,
    submitting,
    error,
    needsManualFields,
    extracted,
  };
}
