'use client';

import { Skeleton } from '@danieljoffe/shared-ui/Skeleton';
import Button from '@/components/kit/Button';
import { cn } from '@/lib/cn';
import JobCard from './JobCard';
import { useJobRemove } from './useJobRemove';
import JobsEmptyState from './JobsEmptyState';
import JobsLoadError from './JobsLoadError';
import type { JobPosting } from './types';

interface JobsListMobileProps {
  postings: JobPosting[];
  loading: boolean;
  hasMore: boolean;
  loadingMore: boolean;
  onLoadMore: () => void;
  selectedIds: Set<string>;
  onSelectionChange: (ids: Set<string>) => void;
  onRefetch: () => void;
  /** Set when the list fetch itself failed — renders the load-error state
   *  instead of the misleading "No jobs found" empty state (#604). */
  loadError?: string | undefined;
  /** Target tab in scope. Removal is per-target; undefined (All Jobs) removes
   *  the posting from every target of the caller's that holds it. */
  targetId?: string | undefined;
}

export default function JobsListMobile({
  postings,
  loading,
  hasMore,
  loadingMore,
  onLoadMore,
  selectedIds,
  onSelectionChange,
  onRefetch,
  loadError,
  targetId,
}: JobsListMobileProps) {
  const { removeJob } = useJobRemove();

  function toggleSelect(id: string) {
    const next = new Set(selectedIds);
    if (next.has(id)) {
      next.delete(id);
    } else {
      next.add(id);
    }
    onSelectionChange(next);
  }

  async function handleDelete(jobId: string) {
    if (await removeJob(jobId, targetId, onRefetch)) onRefetch();
  }

  if (loading && postings.length === 0) {
    return (
      <div className='flex flex-col gap-3' aria-label='Loading jobs'>
        {Array.from({ length: 5 }).map((_, i) => (
          // Mirrors the real <JobCard> shape: title row first (with a small
          // status badge to the right), then a meta row with company + score
          // pill. Prior version put meta before title and added a 2-line text
          // body that doesn't exist in JobCard, leaving empty space on swap.
          <div
            key={i}
            className='flex flex-col gap-2 rounded-xl border border-border bg-surface-elevated p-3'
          >
            <div className='flex items-center justify-between gap-2'>
              <Skeleton width='75%' size='md' />
              <Skeleton variant='rectangular' width={48} height={20} />
            </div>
            <div className='flex items-center gap-2'>
              <Skeleton width={110} size='sm' />
              <Skeleton variant='rectangular' width={36} height={20} />
            </div>
          </div>
        ))}
      </div>
    );
  }

  if (postings.length === 0 && loadError) {
    return <JobsLoadError onRetry={onRefetch} />;
  }

  if (postings.length === 0) {
    return <JobsEmptyState onJobAdded={onRefetch} />;
  }

  // Refetch of an already-loaded list (filter/search/sort): keep the cards
  // mounted (no layout collapse) but dim them and mark the region busy so
  // there's visible + assistive feedback while the new page lands.
  const refetching = loading && postings.length > 0;

  return (
    <div className='flex flex-col gap-3'>
      <ul
        className={cn(
          'flex flex-col gap-3 transition-opacity',
          refetching && 'opacity-50'
        )}
        aria-busy={refetching || undefined}
      >
        {postings.map(job => (
          <li key={job.id}>
            <JobCard
              job={job}
              selected={selectedIds.has(job.id)}
              onSelectToggle={() => toggleSelect(job.id)}
              onDelete={() => handleDelete(job.id)}
            />
          </li>
        ))}
      </ul>

      {hasMore && (
        <div className='mt-2 flex justify-center'>
          <Button
            name='jobs-load-more'
            variant='outline'
            onClick={onLoadMore}
            loading={loadingMore}
            disabled={loadingMore}
          >
            Load more
          </Button>
        </div>
      )}
    </div>
  );
}
