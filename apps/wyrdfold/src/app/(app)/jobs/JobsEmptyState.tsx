'use client';

import { Text } from '@danieljoffe/shared-ui/Text';
import AddJobByUrlButton from './AddJobByUrlButton';

interface JobsEmptyStateProps {
  /**
   * Notify the parent table that a job was added so it can refresh. The
   * parent owns the fetch hook + page state — this component shouldn't
   * try to mutate it directly.
   */
  onJobAdded: () => void;
}

/**
 * Shared empty state for the jobs list (desktop table + mobile cards).
 *
 * Was a static line of text saying "No jobs found. Try adjusting filters
 * or adding jobs manually." — but ``/jobs`` had no actual UI affordance
 * for adding a job manually; the API endpoint (POST /jobs/manual) was
 * only reachable from the onboarding wizard. Users finishing onboarding
 * with the suggest path landed on an empty Top Matches block and had
 * no way to seed one.
 *
 * The add flow itself (button + dialog + error handling) lives in
 * ``AddJobByUrlButton``, which the list toolbar also mounts so the
 * affordance doesn't vanish once the list fills up.
 */
export default function JobsEmptyState({ onJobAdded }: JobsEmptyStateProps) {
  return (
    <div className='flex flex-col items-center gap-3 py-12 text-center'>
      <Text variant='body' className='text-text-tertiary'>
        No jobs found. Try adjusting filters, or paste a posting URL to add one
        manually.
      </Text>
      <AddJobByUrlButton name='jobs-add-manual' onJobAdded={onJobAdded} />
    </div>
  );
}
