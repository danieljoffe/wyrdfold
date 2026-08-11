'use client';

import { useEffect, useRef } from 'react';
import { Spinner } from '@danieljoffe/shared-ui/Spinner';
import Button from '@/components/kit/Button';
import LinkButton from '@/components/kit/LinkButton';
import { useToast } from '@/state/Toast/ToastProvider';
import { loadJobDescription } from './loadJobDescription';
import { isFlaggedDraft } from './types';
import { useTailorDocument } from './useTailorDocument';

interface ResumeSectionProps {
  jobPostingId: string;
}

/**
 * Mirror of ``CoverLetterSection`` for the resume artifact. Distinguishes
 * "no record yet" → renders a Generate button, from "record exists" →
 * renders a Review (or View, once approved) button.
 *
 * The previous inline rendering inside ``JobDetailPanel`` always linked
 * to ``/jobs/{id}/resume`` regardless of whether a tailored doc actually
 * existed, leaving the user staring at a "Resume not found" dead-end
 * page with nowhere to generate one.
 *
 * Generation is non-blocking (#656): ``useTailorDocument`` owns the 202 +
 * poll loop, so the "Generating…" pill reflects a run in flight even when
 * that run was started before this panel mounted (or in another tab), and
 * navigating away mid-generation loses nothing.
 */
export default function ResumeSection({ jobPostingId }: ResumeSectionProps) {
  const { toast } = useToast();
  const { record, loading, generating, error, generate } = useTailorDocument({
    jobPostingId,
    kind: 'resume',
  });

  // Surface background failures as a toast exactly once each. The hook holds
  // the error so a re-render can't re-fire it, but the effect still needs its
  // own guard: `error` staying set across unrelated re-renders would otherwise
  // toast again on every pass.
  const toastedRef = useRef<string | null>(null);
  useEffect(() => {
    if (error && toastedRef.current !== error) {
      toastedRef.current = error;
      toast({ variant: 'error', title: error });
    }
    if (!error) toastedRef.current = null;
  }, [error, toast]);

  async function handleGenerate() {
    // The tailor route requires the JD text alongside ``job_posting_id``
    // — it lives on the posting detail, which list payloads omit.
    const jd = await loadJobDescription(jobPostingId);
    if (!jd.ok) {
      toast({
        variant: 'error',
        title:
          jd.reason === 'empty'
            ? 'Job has no description — cannot tailor a resume.'
            : 'Could not load job description',
      });
      return;
    }

    const ok = await generate({
      job_description: jd.jd,
      job_posting_id: jobPostingId,
    });
    if (ok) {
      toast({ variant: 'success', title: 'Tailored resume drafted with AI' });
    }
  }

  if (loading) {
    return (
      <Button
        name='resume-loading'
        variant='secondary'
        size='sm'
        disabled
        title='Checking for an existing draft…'
      >
        Resume…
      </Button>
    );
  }

  const isApproved = record?.approved_at != null;
  const flagged = isFlaggedDraft(record);

  // Single toolbar pill: the button verb conveys both state and action
  // ("Generate Resume" implies no draft exists; "Review Resume" implies one
  // does; "View Resume" once approved).
  if (generating) {
    return (
      <Button
        name='resume-generating'
        variant='secondary'
        size='sm'
        disabled
        title='Tailoring in progress — usually 30–60 seconds. Safe to navigate away.'
      >
        <Spinner size='sm' aria-label='Generating resume' />
        <span>Generating…</span>
      </Button>
    );
  }
  if (!record) {
    return (
      <Button
        name='generate-resume'
        variant='primary'
        size='sm'
        onClick={handleGenerate}
      >
        Generate tailored resume
      </Button>
    );
  }
  return (
    <LinkButton
      href={`/jobs/${jobPostingId}/resume`}
      variant={isApproved ? 'secondary' : 'primary'}
      size='sm'
      name={
        isApproved
          ? 'view-approved-resume'
          : flagged
            ? 'fix-flagged-resume'
            : 'review-resume'
      }
      // A flagged draft still exists and is editable — the label says so
      // rather than hiding it, because the whole point of persisting it is
      // that the user can fix it without paying to regenerate (#656).
      title={
        flagged ? 'This draft failed ATS checks — open it to fix' : undefined
      }
    >
      {isApproved
        ? 'View tailored resume'
        : flagged
          ? 'Fix tailored resume'
          : 'Review tailored resume'}
    </LinkButton>
  );
}
