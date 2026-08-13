'use client';

import { useEffect, useRef } from 'react';
import { Spinner } from '@danieljoffe/shared-ui/Spinner';
import Button from '@/components/kit/Button';
import LinkButton from '@/components/kit/LinkButton';
import { useToast } from '@/state/Toast/ToastProvider';
import { loadJobDescription } from './loadJobDescription';
import { isFlaggedDraft } from './types';
import { useTailorDocument } from './useTailorDocument';

interface CoverLetterSectionProps {
  jobPostingId: string;
  companyName: string;
  roleTitle: string;
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
}: CoverLetterSectionProps) {
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
      <Button
        name='generate-cover-letter'
        variant='secondary'
        size='sm'
        onClick={handleGenerate}
      >
        Generate cover letter
      </Button>
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
      {isApproved
        ? 'View Cover Letter'
        : flagged
          ? 'Fix Cover Letter'
          : 'Review Cover Letter'}
    </LinkButton>
  );
}
