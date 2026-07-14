import type { Metadata } from 'next';
import ResumeReviewPage from './ResumeReviewPage';

export const metadata: Metadata = {
  title: 'Review tailored resume',
};

export default async function FittedResumeReview({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <ResumeReviewPage jobPostingId={id} />;
}
