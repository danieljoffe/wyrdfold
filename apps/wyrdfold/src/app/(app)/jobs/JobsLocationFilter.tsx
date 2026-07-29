'use client';

import { useEffect, useId, useState } from 'react';
import { ChevronDown, MapPin } from 'lucide-react';
import {
  Popover,
  type PopoverTriggerProps,
} from '@danieljoffe/shared-ui/Popover';
import { cn } from '@/lib/cn';

const PILL_CLASS =
  'inline-flex items-center gap-1.5 rounded-full border border-border bg-surface-elevated px-3 py-1.5 text-xs text-text-primary hover:bg-surface-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500';

const PILL_ACTIVE_CLASS =
  'border-brand-500/60 bg-brand-500/10 text-text-primary';

interface JobsLocationFilterProps {
  only: string;
  exclude: string;
  onChange: (next: { only: string; exclude: string }) => void;
}

/**
 * Location filter popover for /jobs. Composed on shared-ui Popover (#485,
 * 0.10+ composable trigger): the primitive owns open state, outside-click and
 * Escape dismiss, and focus management; we keep the styled filter pill (with
 * its active-count highlight) via the render-function `trigger`, and commit
 * the drafts on EVERY dismissal via `onOpenChange(false)` — outside click,
 * Escape, and trigger re-click all commit uniformly (commit-on-dismiss).
 */
export default function JobsLocationFilter({
  only,
  exclude,
  onChange,
}: JobsLocationFilterProps) {
  const [onlyDraft, setOnlyDraft] = useState(only);
  const [excludeDraft, setExcludeDraft] = useState(exclude);
  const onlyId = useId();
  const excludeId = useId();

  // Sync drafts when parent filters change (e.g. tab switch clears them).
  useEffect(() => setOnlyDraft(only), [only]);
  useEffect(() => setExcludeDraft(exclude), [exclude]);

  function commit() {
    if (onlyDraft !== only || excludeDraft !== exclude) {
      onChange({ only: onlyDraft, exclude: excludeDraft });
    }
  }

  const activeCount = (only ? 1 : 0) + (exclude ? 1 : 0);
  const label =
    activeCount === 0
      ? 'Locations'
      : activeCount === 1
        ? 'Locations · 1'
        : 'Locations · 2';

  return (
    <Popover
      align='right'
      aria-label='Filter by location'
      panelClassName='w-80 max-w-[calc(100vw-2rem)] p-3'
      onOpenChange={next => {
        if (!next) commit();
      }}
      trigger={(triggerProps: PopoverTriggerProps) => (
        <button
          {...triggerProps}
          className={cn(PILL_CLASS, activeCount > 0 && PILL_ACTIVE_CLASS)}
        >
          <MapPin className='size-3.5 text-text-tertiary' aria-hidden />
          {label}
          <ChevronDown className='size-3 text-text-tertiary' aria-hidden />
        </button>
      )}
    >
      <div className='flex flex-col gap-3'>
        <div className='flex flex-col gap-1'>
          <label
            htmlFor={onlyId}
            className='text-xs font-medium text-text-secondary'
          >
            Only show jobs in
          </label>
          <input
            id={onlyId}
            type='text'
            value={onlyDraft}
            onChange={e => setOnlyDraft(e.target.value)}
            onBlur={commit}
            placeholder='Remote, US'
            className='rounded-md border border-border bg-surface-base px-2.5 py-1.5 text-xs text-text-primary placeholder:text-text-tertiary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500'
          />
        </div>
        <div className='flex flex-col gap-1'>
          <label
            htmlFor={excludeId}
            className='text-xs font-medium text-text-secondary'
          >
            Hide jobs in
          </label>
          <input
            id={excludeId}
            type='text'
            value={excludeDraft}
            onChange={e => setExcludeDraft(e.target.value)}
            onBlur={commit}
            placeholder='India, Brazil'
            className='rounded-md border border-border bg-surface-base px-2.5 py-1.5 text-xs text-text-primary placeholder:text-text-tertiary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500'
          />
        </div>
        <p className='text-[11px] text-text-tertiary'>
          Comma-separated. Case-insensitive substring match against the
          job&apos;s location.
        </p>
      </div>
    </Popover>
  );
}
