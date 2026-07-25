'use client';

import Link from 'next/link';
import { ArrowRight } from 'lucide-react';

import ListingDetailBody from '../ListingDetailBody';
import type { JobSearchResult } from '../types';
import { useListingBindings } from '../useListingBindings';

/**
 * The hard-load frame for a shared listing URL (#467 §11.2 fast-follow): the
 * SAME {@link ListingDetailBody} the modal shows, framed as a page — a bounded
 * card column under the auth-adaptive `/search` shell — plus the "Browse more
 * jobs" on-ramp into the grid (the shared link is the funnel's entry point;
 * the grid is where it converts). Membership + the targets picker ride
 * {@link useListingBindings}, so bind→unlock behaves identically to the modal.
 */
export default function ListingPageContent({
  job,
  isAuthenticated,
}: {
  job: JobSearchResult;
  isAuthenticated: boolean;
}) {
  const { inTargets, targetsSource, handleAdded } = useListingBindings(
    job.id,
    isAuthenticated
  );

  return (
    <div className='mx-auto flex w-full max-w-2xl flex-col gap-4'>
      <div className='rounded-lg border border-border bg-surface-elevated p-6 shadow-sm'>
        <ListingDetailBody
          job={job}
          inTargets={inTargets}
          isAuthenticated={isAuthenticated}
          targetsSource={targetsSource}
          onAddedToTarget={handleAdded}
        />
      </div>
      <Link
        href='/search'
        className='inline-flex items-center gap-1 self-start text-sm font-semibold text-text-brand underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2'
      >
        Browse more jobs
        <ArrowRight className='size-3.5 shrink-0' aria-hidden />
      </Link>
    </div>
  );
}
