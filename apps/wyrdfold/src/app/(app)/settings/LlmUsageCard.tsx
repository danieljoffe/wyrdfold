'use client';

import { useEffect, useState } from 'react';
import { Gauge } from 'lucide-react';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@danieljoffe/shared-ui/Card';
import { ProgressBar } from '@danieljoffe/shared-ui/ProgressBar';
import { Skeleton } from '@danieljoffe/shared-ui/Skeleton';
import { Text } from '@danieljoffe/shared-ui/Text';
import { LocalDate } from '@/components/LocalFormat';

interface UsageWindow {
  spent_usd: number;
  limit_usd: number;
}

interface LlmUsage {
  hourly: UsageWindow;
  daily: UsageWindow;
  monthly: UsageWindow;
  monthly_resets_at: string | null;
  analysis_daily_used: number;
  analysis_daily_limit: number;
}

/** Amber past 70%, red past 90% — the same tiers the hand-rolled meter used,
 *  now expressed as the shared ProgressBar's `variant` (accent = the brand fill).
 *  Exported for unit tests to lock the tier boundaries + the zero-limit case. */
export function usageVariant(
  spent: number,
  limit: number
): 'accent' | 'warning' | 'error' {
  const pct = limit > 0 ? (spent / limit) * 100 : 0;
  return pct >= 90 ? 'error' : pct >= 70 ? 'warning' : 'accent';
}

export default function LlmUsageCard() {
  const [usage, setUsage] = useState<LlmUsage | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const res = await fetch('/api/profile/llm-usage');
        if (!res.ok) throw new Error(`${res.status}`);
        const data = (await res.json()) as LlmUsage;
        if (!cancelled) setUsage(data);
      } catch {
        if (!cancelled) setFailed(true);
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <Card>
      <CardHeader>
        <CardTitle className='flex items-center gap-2'>
          <Gauge className='size-4' aria-hidden />
          AI usage
        </CardTitle>
      </CardHeader>
      <CardContent className='flex flex-col gap-3'>
        {failed ? (
          <Text variant='caption' className='text-text-secondary'>
            Usage data is unavailable right now.
          </Text>
        ) : usage === null ? (
          <div className='flex flex-col gap-2' aria-label='Loading usage'>
            <Skeleton width='40%' size='sm' />
            <Skeleton variant='rectangular' height={8} />
          </div>
        ) : (
          <>
            <div className='flex items-baseline justify-between'>
              <Text variant='caption' className='text-text-secondary'>
                Monthly allowance
              </Text>
              <Text variant='caption'>
                ${usage.monthly.spent_usd.toFixed(2)} of $
                {usage.monthly.limit_usd.toFixed(2)}
              </Text>
            </div>
            <ProgressBar
              // A 0 limit would make ProgressBar read 100% (value/max → ∞,
              // clamped to full); the old meter showed an EMPTY bar for a
              // no-limit account, so pin the zero-limit case to 0%.
              value={usage.monthly.limit_usd > 0 ? usage.monthly.spent_usd : 0}
              max={usage.monthly.limit_usd > 0 ? usage.monthly.limit_usd : 100}
              variant={usageVariant(
                usage.monthly.spent_usd,
                usage.monthly.limit_usd
              )}
              size='md'
              aria-label='Monthly allowance used'
            />
            <div className='flex items-baseline justify-between'>
              <Text variant='caption' className='text-text-secondary'>
                Deep analyses today
              </Text>
              <Text variant='caption'>
                {usage.analysis_daily_used} of {usage.analysis_daily_limit}
              </Text>
            </div>
            {usage.monthly_resets_at && (
              <Text variant='caption' className='text-text-tertiary'>
                Allowance frees up around{' '}
                <LocalDate value={usage.monthly_resets_at} /> as usage rolls out
                of the 30-day window.
              </Text>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}
