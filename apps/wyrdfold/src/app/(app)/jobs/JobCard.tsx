'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { ExternalLink, Maximize2, MoreVertical, Trash2 } from 'lucide-react';
import { Badge } from '@danieljoffe/shared-ui/Badge';
import { Dropdown } from '@danieljoffe/shared-ui/Dropdown';
import type { DropdownItem } from '@danieljoffe/shared-ui/Dropdown';
import ConfirmModal from '@/components/ConfirmModal';
import ScoreBadge from '@/components/ScoreBadge';
import { cn } from '@/lib/cn';
import LogisticsChips from './LogisticsChips';
import StatusIndicator from './StatusIndicator';
import { MANUAL_SOURCE_ID, type JobPosting } from './types';

interface JobCardProps {
  job: JobPosting;
  selected: boolean;
  onSelectToggle: () => void;
  onDelete: () => void;
}

function timeAgo(dateStr: string | null): string {
  if (!dateStr) return '—';
  const diff = Date.now() - new Date(dateStr).getTime();
  const days = Math.floor(diff / 86400000);
  if (days === 0) return 'today';
  if (days === 1) return '1d ago';
  return `${days}d ago`;
}

export default function JobCard({
  job,
  selected,
  onSelectToggle,
  onDelete,
}: JobCardProps) {
  const router = useRouter();
  const detailHref = `/jobs/${job.id}`;
  const [confirmDeleteOpen, setConfirmDeleteOpen] = useState(false);

  const items: DropdownItem[] = [
    {
      label: 'Open full view',
      icon: <Maximize2 className='size-4' aria-hidden />,
      onClick: () => router.push(detailHref),
    },
    ...(job.absolute_url
      ? [
          {
            label: 'View original post',
            icon: <ExternalLink className='size-4' aria-hidden />,
            onClick: () =>
              window.open(
                job.absolute_url ?? '',
                '_blank',
                'noopener,noreferrer'
              ),
          },
        ]
      : []),
    { label: '', divider: true },
    {
      label: 'Delete',
      icon: <Trash2 className='size-4' aria-hidden />,
      danger: true,
      onClick: () => setConfirmDeleteOpen(true),
    },
  ];

  function handleNavigate() {
    router.push(detailHref);
  }

  return (
    <article
      className={cn(
        'flex flex-col gap-2.5 rounded-xl border bg-surface-elevated p-3 transition-colors',
        'cursor-pointer hover:bg-surface-secondary',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2',
        selected ? 'border-brand-500' : 'border-border'
      )}
      onClick={handleNavigate}
      onKeyDown={e => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          handleNavigate();
        }
      }}
      tabIndex={0}
      role='button'
      aria-label={`${job.title} at ${job.company_name}`}
    >
      <header className='flex items-start justify-between gap-2'>
        <div className='flex min-w-0 items-center gap-2'>
          <input
            type='checkbox'
            checked={selected}
            onChange={onSelectToggle}
            onClick={e => e.stopPropagation()}
            aria-label={`Select ${job.title}`}
            className='mt-0.5 shrink-0 accent-brand-500'
          />
          <ScoreBadge score={job.score} scoringStatus={job.scoring_status} />
          <span className='truncate text-sm font-medium leading-tight text-text-primary'>
            {job.title}
          </span>
        </div>
        <div onClick={e => e.stopPropagation()}>
          <Dropdown
            trigger={
              <span className='inline-flex rounded p-1 text-text-secondary hover:bg-surface-tertiary hover:text-text-primary'>
                <MoreVertical className='size-4' aria-hidden />
              </span>
            }
            items={items}
            align='right'
          />
        </div>
      </header>

      <LogisticsChips filters={job.logistics_filters} className='px-0.5' />

      <hr className='-mx-3 border-border' />

      <dl className='grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs'>
        <dt className='text-text-tertiary'>Company</dt>
        <dd className='flex min-w-0 items-center justify-end gap-2 text-text-secondary'>
          <span className='truncate font-medium'>{job.company_name}</span>
          {job.source_id === MANUAL_SOURCE_ID && (
            <Badge variant='info' size='sm'>
              Discovered
            </Badge>
          )}
        </dd>
        <dt className='text-text-tertiary'>Location</dt>
        <dd className='truncate text-right text-text-secondary'>
          {job.location ?? '—'}
        </dd>
        <dt className='text-text-tertiary'>Salary</dt>
        <dd className='truncate text-right text-text-secondary'>
          {job.salary_text ?? '—'}
        </dd>
        <dt className='text-text-tertiary'>Posted</dt>
        <dd className='text-right text-text-secondary'>
          {timeAgo(job.created_at)}
        </dd>
      </dl>

      <hr className='-mx-3 border-border' />

      <div className='flex justify-end'>
        <StatusIndicator status={job.status} />
      </div>

      {/* Stop clicks inside the dialog (and its backdrop) from bubbling up
          to the article's navigate handler. */}
      <div onClick={e => e.stopPropagation()}>
        <ConfirmModal
          isOpen={confirmDeleteOpen}
          onClose={() => setConfirmDeleteOpen(false)}
          onConfirm={() => {
            setConfirmDeleteOpen(false);
            onDelete();
          }}
          title='Delete posting?'
          message={`Delete "${job.title}" from ${job.company_name}? This can't be undone.`}
          confirmLabel='Delete'
          destructive
        />
      </div>
    </article>
  );
}
