'use client';

import { useCallback, useEffect, useState, useTransition } from 'react';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';

import { cn } from '@/lib/cn';

import type { HomeView } from './view';

// Label is "Overview", id stays 'today' (URL-stable). The section shows
// lifetime pipeline counts (status=new, saved, drafts…) — labeling it
// "Today" read those as daily figures ("New matches 7340" under a Today
// tab; ux-sweep 2026-08-12 §B5).
const VIEWS: { id: HomeView; label: string }[] = [
  { id: 'today', label: 'Overview' },
  { id: 'trends', label: 'Trends' },
];

/**
 * Home's section toggle (UX/IA Fork A). "Today" is the daily launcher
 * (the old Dashboard); "Trends" is the historical view (the old
 * /insights page, now a section of Home rather than a peer route).
 * The choice lives in the URL (``?view=``) so it's shareable, sticky
 * across navigation, and the server picks which section's data to
 * fetch — only one section is fetched per request.
 */
export default function HomeViewToggle({ value }: { value: HomeView }) {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();

  // Optimistic selected state (#605, evidence in #601): deriving the
  // active button purely from the committed URL made the control lag the
  // click by the full server round-trip — measured at ~4s during the
  // 2026-08-05 drive, which reads as a dead button and invites
  // rage-clicks. The highlight flips on click; the effect reconciles it
  // whenever the server-committed value changes underneath (back/forward,
  // external navigation).
  const [isPending, startTransition] = useTransition();
  const [optimisticValue, setOptimisticValue] = useState(value);
  useEffect(() => {
    setOptimisticValue(value);
  }, [value]);

  const handleChange = useCallback(
    (next: HomeView) => {
      setOptimisticValue(next);
      startTransition(() => {
        const params = new URLSearchParams(searchParams.toString());
        if (next === 'today') {
          params.delete('view');
        } else {
          params.set('view', next);
        }
        const qs = params.toString();
        router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
      });
    },
    [pathname, router, searchParams]
  );

  return (
    <div
      role='group'
      aria-label='Home view'
      aria-busy={isPending || undefined}
      className='flex w-fit gap-1 p-1 bg-surface-tertiary rounded-lg'
    >
      {VIEWS.map(v => (
        <button
          key={v.id}
          type='button'
          onClick={() => handleChange(v.id)}
          aria-pressed={optimisticValue === v.id}
          className={cn(
            'px-4 py-1.5 rounded-md text-sm font-medium transition-colors',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2',
            optimisticValue === v.id
              ? 'bg-surface text-text-primary shadow-sm'
              : 'text-text-secondary hover:text-text-primary'
          )}
        >
          {v.label}
        </button>
      ))}
    </div>
  );
}
