import { Text } from '@danieljoffe/shared-ui/Text';
import type { JobPosting } from './types';

interface JobsPendingOnlyNoteProps {
  postings: JobPosting[];
  loading: boolean;
  /** The active min-score chip value ('' when no explicit filter is set). */
  minScore: string;
}

/**
 * Ungraded rows are deliberately exempt from the score floor (#47) — hiding
 * them would let the daily grading cap bury new matches. But under a HIGH
 * explicit score chip that design reads as a bug: the sweep (ux-sweep
 * 2026-08-12 §A3) found "Score 85+" returning a page of blank score badges
 * with nothing explaining why. When an explicit score filter yields ONLY
 * pending rows, say so instead of letting the list look broken.
 */
export default function JobsPendingOnlyNote({
  postings,
  loading,
  minScore,
}: JobsPendingOnlyNoteProps) {
  const threshold = Number(minScore);
  if (!minScore || !Number.isFinite(threshold) || threshold <= 0) return null;
  if (loading || postings.length === 0) return null;
  if (!postings.every(p => p.pending)) return null;

  return (
    <Text variant='caption' as='p' className='text-text-secondary' role='note'>
      Every job matching this filter is still waiting for its full match grade
      (the &middot; badge). The score filter applies only to graded jobs —
      ungraded ones stay visible so new matches aren&rsquo;t hidden while
      grading catches up.
    </Text>
  );
}
