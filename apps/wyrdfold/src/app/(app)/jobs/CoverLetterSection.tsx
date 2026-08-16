'use client';

import { useEffect, useRef, useState } from 'react';
import { Spinner } from '@danieljoffe/shared-ui/Spinner';
import Button from '@/components/kit/Button';
import ConfirmModal from '@/components/ConfirmModal';
import LinkButton from '@/components/kit/LinkButton';
import { useToast } from '@/state/Toast/ToastProvider';
import { loadJobDescription } from './loadJobDescription';
import { isFlaggedDraft } from './types';
import { useTailorDocument } from './useTailorDocument';

interface CoverLetterSectionProps {
  jobPostingId: string;
  companyName: string;
  roleTitle: string;
  /** The match analysis' recommendation, when it advises skipping. Present =
   *  confirm before spending. Generation is billed per run, and a Skip job is
   *  exactly where the model may decline to apply on the user's behalf — so
   *  being charged for a refusal with no warning is the worst case. */
  skipReason?: string | undefined;
}

/**
 * Cover-letter twin of ``ResumeSection``. Generation is non-blocking (#656):
 * ``useTailorDocument`` owns the 202 + poll loop, so the pill reflects a run
 * in flight even across a navigation.
 */
export default function CoverLetterSection({
  jobPostingId,
  companyName,
  roleTitle,
  skipReason,
}: CoverLetterSectionProps) {
  const [confirmOpen, setConfirmOpen] = useState(false);
  const { toast } = useToast();
  const { record, loading, generating, error, generate } = useTailorDocument({
    jobPostingId,
    kind: 'cover_letter',
  });

  // One toast per distinct failure — see ResumeSection for the guard rationale.
  const toastedRef = useRef<string | null>(null);
  useEffect(() => {
    if (error && toastedRef.current !== error) {
      toastedRef.current = error;
      toast({ variant: 'error', title: error });
    }
    if (!error) toastedRef.current = null;
  }, [error, toast]);

  /** Gate a billed run behind a confirm when the analysis says skip. */
  function requestGenerate() {
    if (skipReason) {
      setConfirmOpen(true);
      return;
    }
    void handleGenerate();
  }

  async function handleGenerate() {
    const jd = await loadJobDescription(jobPostingId);
    if (!jd.ok) {
      toast({
        variant: 'error',
        title:
          jd.reason === 'empty'
            ? 'Job has no description — cannot tailor a cover letter.'
            : 'Could not load job description',
      });
      return;
    }

    const ok = await generate({
      job_description: jd.jd,
      job_posting_id: jobPostingId,
      company_name: companyName,
      role_title: roleTitle,
    });
    if (ok) {
      toast({ variant: 'success', title: 'Cover letter generated' });
    }
  }

  if (loading) {
    return (
      <Button
        name='cover-letter-loading'
        variant='secondary'
        size='sm'
        disabled
      >
        Cover letter…
      </Button>
    );
  }

  const isApproved = record?.approved_at != null;
  const flagged = isFlaggedDraft(record);

  // Single toolbar pill: the button verb conveys both state and action.
  // See ResumeSection for the rationale.
  if (generating) {
    return (
      <Button
        name='cover-letter-generating'
        variant='secondary'
        size='sm'
        disabled
        title='Generating — safe to navigate away.'
      >
        <Spinner size='sm' aria-label='Generating cover letter' />
        <span>Generating…</span>
      </Button>
    );
  }
  if (!record) {
    return (
      <>
        <Button
          name='generate-cover-letter'
          variant='secondary'
          size='sm'
          onClick={requestGenerate}
        >
          Generate cover letter
        </Button>
        {confirmOpen && (
          <ConfirmModal
            isOpen
            onClose={() => setConfirmOpen(false)}
            onConfirm={() => {
              setConfirmOpen(false);
              void handleGenerate();
            }}
            title='Generate anyway?'
            message={`The match analysis recommends skipping this one: "${skipReason}" Generating a cover letter is billed per run.`}
            confirmLabel='Generate anyway'
            cancelLabel='Cancel'
            name='cover-letter-spend-confirm'
          />
        )}
      </>
    );
  }
  return (
    <LinkButton
      href={`/jobs/${jobPostingId}/cover-letter`}
      variant={isApproved ? 'secondary' : 'primary'}
      size='sm'
      name={
        isApproved
          ? 'review-cover-letter'
          : flagged
            ? 'fix-flagged-cover-letter'
            : 'review-cover-letter'
      }
      title={
        flagged ? 'This draft failed ATS checks — open it to fix' : undefined
      }
    >
      {/* Sentence case, matching ResumeSection's "Review tailored resume" —
          the two buttons sit side by side in the same toolbar. */}
      {isApproved
        ? 'View cover letter'
        : flagged
          ? 'Fix cover letter'
          : 'Review cover letter'}
    </LinkButton>
  );
}
