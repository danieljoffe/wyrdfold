'use client';

import { Fragment, useEffect, useState } from 'react';
import { ExternalLink } from 'lucide-react';
import { formatJobSalary } from '@/lib/formatSalary';
import { displayTitle } from '@/lib/displayTitle';
import { formatCompanyName } from '@/lib/formatCompanyName';
import { Badge } from '@danieljoffe/shared-ui/Badge';
import { Checkbox } from '@danieljoffe/shared-ui/Checkbox';
import Button from '@/components/kit/Button';
import ScoreBadge from '@/components/ScoreBadge';
import { formatLocation } from '@/lib/formatLocation';
import { cn } from '@/lib/cn';
import { timeAgo } from '@/lib/timeAgo';
import JobDetailPanel from './JobDetailPanel';
import JobsEmptyState from './JobsEmptyState';
import JobsLoadError from './JobsLoadError';
import LogisticsChips from './LogisticsChips';
import JobsTableSkeleton from './JobsTableSkeleton';
import StatusIndicator from './StatusIndicator';
import {
  MANUAL_SOURCE_ID,
  type JobPosting,
  type JobsSortColumn,
  postedAt,
} from './types';

interface JobsListTableProps {
  postings: JobPosting[];
  loading: boolean;
  hasMore: boolean;
  loadingMore: boolean;
  onLoadMore: () => void;
  sort: JobsSortColumn;
  order: 'asc' | 'desc';
  handleSort: (col: JobsSortColumn) => void;
  sortIndicator: (col: JobsSortColumn) => string;
  selectedIds: Set<string>;
  onSelectionChange: (ids: Set<string>) => void;
  analysisTargetId: string | undefined;
  onRefetch: () => void;
  /** Set when the list fetch itself failed — renders the load-error state
   *  instead of the misleading "No jobs found" empty state (#604). */
  loadError?: string | undefined;
}

const COLUMNS: { key: JobsSortColumn; label: string }[] = [
  { key: 'score', label: 'Score' },
  { key: 'title', label: 'Title' },
  { key: 'company_name', label: 'Company' },
  { key: 'created_at', label: 'Posted' },
];

export default function JobsListTable({
  postings,
  loading,
  hasMore,
  loadingMore,
  onLoadMore,
  sort: activeSort,
  order: sortOrder,
  handleSort,
  sortIndicator,
  selectedIds,
  onSelectionChange,
  analysisTargetId,
  onRefetch,
  loadError,
}: JobsListTableProps) {
  const [expandedId, setExpandedId] = useState<string | null>(null);
  // Snapshot of the expanded posting so the open panel survives a refetch
  // that re-ranks the job out of the current page (#602). The panel's own
  // ``onAnalysisComplete`` triggers that refetch, and a fit grade landing
  // is exactly when the row is most likely to drop out of the top-N — so
  // without the snapshot the user's in-flight analysis/tailoring panel
  // unmounts mid-read.
  const [expandedSnapshot, setExpandedSnapshot] = useState<JobPosting | null>(
    null
  );

  function toggleExpand(job: JobPosting) {
    if (expandedId === job.id) {
      setExpandedId(null);
      setExpandedSnapshot(null);
    } else {
      setExpandedId(job.id);
      setExpandedSnapshot(job);
    }
  }

  // While the expanded job is still on the page, track its freshest copy so
  // a later pin shows updated fields (score, status) rather than the values
  // from expansion time.
  useEffect(() => {
    if (!expandedId) return;
    const fresh = postings.find(p => p.id === expandedId);
    if (fresh) setExpandedSnapshot(fresh);
  }, [postings, expandedId]);

  // Pin the snapshot into the rendered list when the expanded job has left
  // the page. (If the whole list empties — e.g. the user changes filters —
  // the empty state below still wins; the pin only augments a rendered
  // table.)
  const pinnedJob =
    expandedId &&
    expandedSnapshot &&
    expandedSnapshot.id === expandedId &&
    !postings.some(p => p.id === expandedId)
      ? expandedSnapshot
      : null;
  const displayPostings = pinnedJob ? [pinnedJob, ...postings] : postings;

  const allOnPageSelected =
    postings.length > 0 && postings.every(p => selectedIds.has(p.id));

  function toggleSelectAll() {
    const next = new Set(selectedIds);
    if (allOnPageSelected) {
      for (const p of postings) next.delete(p.id);
    } else {
      for (const p of postings) next.add(p.id);
    }
    onSelectionChange(next);
  }

  function toggleSelect(id: string) {
    const next = new Set(selectedIds);
    if (next.has(id)) {
      next.delete(id);
    } else {
      next.add(id);
    }
    onSelectionChange(next);
  }

  if (loading && postings.length === 0) {
    return <JobsTableSkeleton />;
  }

  if (postings.length === 0 && loadError) {
    return <JobsLoadError onRetry={onRefetch} />;
  }

  if (postings.length === 0) {
    return <JobsEmptyState onJobAdded={onRefetch} />;
  }

  // Refetch of an already-loaded list (filter/search/sort): keep the rows
  // mounted (no layout collapse) but dim them and mark the region busy so
  // there's visible + assistive feedback while the new page lands.
  const refetching = loading && postings.length > 0;

  return (
    <div>
      <div
        className={cn(
          'overflow-x-auto transition-opacity',
          refetching && 'opacity-50'
        )}
        aria-busy={refetching || undefined}
      >
        <table className='w-full text-sm' aria-label='Job postings'>
          <thead>
            <tr className='border-b border-border text-left'>
              <th scope='col' className='px-3 py-2 w-10'>
                <Checkbox
                  checked={allOnPageSelected}
                  onChange={toggleSelectAll}
                  aria-label='Select all on this page'
                />
              </th>
              <th
                scope='col'
                className='px-3 py-2 font-medium text-text-secondary'
              >
                Status
              </th>
              {COLUMNS.map(col => (
                <th
                  key={col.key}
                  scope='col'
                  className='px-3 py-2'
                  aria-sort={
                    activeSort === col.key
                      ? sortOrder === 'asc'
                        ? 'ascending'
                        : 'descending'
                      : undefined
                  }
                >
                  <button
                    type='button'
                    className='flex items-center gap-1 font-medium text-text-secondary hover:text-text-primary'
                    onClick={() => handleSort(col.key)}
                    aria-label={`Sort by ${col.label}`}
                  >
                    {col.label} {sortIndicator(col.key)}
                  </button>
                </th>
              ))}
              <th
                scope='col'
                className='px-3 py-2 font-medium text-text-secondary'
              >
                Salary
              </th>
              <th
                scope='col'
                className='px-3 py-2 font-medium text-text-secondary'
              >
                Location
              </th>
            </tr>
          </thead>
          <tbody>
            {displayPostings.map(job => (
              <Fragment key={job.id}>
                <tr
                  className={cn(
                    'border-b border-border hover:bg-surface-secondary cursor-pointer transition-colors',
                    expandedId === job.id && 'bg-surface-secondary'
                  )}
                  onClick={() => toggleExpand(job)}
                  onKeyDown={e => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault();
                      toggleExpand(job);
                    }
                  }}
                  tabIndex={0}
                  role='row'
                  aria-expanded={expandedId === job.id}
                  aria-controls={`job-detail-${job.id}`}
                  aria-label={`${displayTitle(job)} at ${job.company_name}, press Enter to ${expandedId === job.id ? 'collapse' : 'expand'} details`}
                >
                  <td className='px-3 py-2'>
                    {/* stopPropagation on the wrapper, not the control: the
                        shared Checkbox's visible box is a separate element whose
                        click would otherwise bubble to the row's expand handler. */}
                    <span onClick={e => e.stopPropagation()}>
                      <Checkbox
                        checked={selectedIds.has(job.id)}
                        onChange={() => toggleSelect(job.id)}
                        aria-label={`Select ${displayTitle(job)}`}
                      />
                    </span>
                  </td>
                  <td className='px-3 py-2'>
                    <StatusIndicator status={job.status} />
                  </td>
                  <td className='px-3 py-2'>
                    <ScoreBadge
                      score={job.score}
                      scoringStatus={job.scoring_status}
                      pending={job.pending}
                    />
                  </td>
                  <td className='px-3 py-2 font-medium'>
                    <div className='flex flex-col gap-1'>
                      {/* The title used to BE the outbound ATS link, so the
                          most obvious click target on the row left the app —
                          past the score breakdown, match analysis and
                          tailoring the user came for. The title now expands
                          the row (the <tr> handler); applying keeps its own
                          explicit icon. */}
                      <span className='inline-flex items-center gap-2'>
                        <span className='text-text-primary'>
                          {displayTitle(job)}
                        </span>
                        {job.absolute_url && (
                          <a
                            href={job.absolute_url}
                            target='_blank'
                            rel='noopener noreferrer'
                            className='text-brand-500 hover:text-brand-600'
                            onClick={e => e.stopPropagation()}
                            aria-label={`Open ${displayTitle(job)} at ${formatCompanyName(job.company_name)} on the employer's site (opens in a new tab)`}
                          >
                            <ExternalLink className='h-3.5 w-3.5' />
                          </a>
                        )}
                        {job.source_id === MANUAL_SOURCE_ID && (
                          <Badge variant='info'>Discovered</Badge>
                        )}
                      </span>
                      {/* Compact logistics chips inline under the title (#86) —
                          the mobile card + detail panel already show these; the
                          desktop table row was the gap. Renders nothing when the
                          job has no logistics data. */}
                      <LogisticsChips filters={job.logistics_filters} />
                    </div>
                  </td>
                  <td className='px-3 py-2'>
                    {formatCompanyName(job.company_name)}
                  </td>
                  <td className='px-3 py-2 text-text-tertiary'>
                    {timeAgo(postedAt(job))}
                  </td>
                  <td className='px-3 py-2 text-text-tertiary'>
                    {formatJobSalary(job) ?? '—'}
                  </td>
                  <td
                    className='px-3 py-2 text-text-tertiary truncate max-w-[150px]'
                    title={formatLocation(job) || undefined}
                  >
                    {formatLocation(job) || '—'}
                  </td>
                </tr>
                {expandedId === job.id && (
                  <tr>
                    <td colSpan={8} className='p-0' id={`job-detail-${job.id}`}>
                      {pinnedJob?.id === job.id && (
                        <div
                          role='status'
                          className='border-b border-border bg-surface-tertiary px-4 py-2 text-xs text-text-secondary'
                        >
                          Score updated — this job re-ranked out of the current
                          list. It stays open here until you close it.
                        </div>
                      )}
                      <JobDetailPanel
                        posting={job}
                        targetId={analysisTargetId}
                        viewFullHref={`/jobs/${job.id}`}
                        onDelete={() => {
                          setExpandedId(null);
                          setExpandedSnapshot(null);
                          onRefetch();
                        }}
                        onStatusChange={() => onRefetch()}
                        onAnalysisComplete={onRefetch}
                      />
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
      {hasMore && (
        <div className='mt-4 flex justify-center'>
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
