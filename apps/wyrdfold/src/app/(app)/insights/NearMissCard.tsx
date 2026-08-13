'use client';

import { useEffect, useState } from 'react';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@danieljoffe/shared-ui/Card';
import { Badge } from '@danieljoffe/shared-ui/Badge';
import { Skeleton } from '@danieljoffe/shared-ui/Skeleton';
import { Text } from '@danieljoffe/shared-ui/Text';
import type { NearMissInsights } from './types';

/**
 * "Almost matched" — low-confidence Phase-1 rejections per target (#703 f/u).
 *
 * These are titles the triage gate said "no" to WITHOUT being sure: roles
 * adjacent to the target. Two readings, both actionable: postings the user
 * may want the target widened to include, or a sign the target label reads
 * narrower than intended. Mined for free from verdicts the pipeline already
 * paid for — no LLM call happens on this path.
 *
 * Self-contained (own fetch/loading/empty states) rather than a fourth
 * slice in ``useInsights``: that hook is period-driven and server-seeded,
 * while this card has no period dimension — a fixed recent window is part
 * of the API contract. Fetches once per mount.
 *
 * Silent on failure: an advisory card must never add an error banner to
 * the dashboard. Hidden entirely when every target has an empty list —
 * "no near-misses" is the healthy steady state, not an empty state worth
 * narrating.
 */
export default function NearMissCard() {
  const [data, setData] = useState<NearMissInsights | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const res = await fetch('/api/jobs/insights/near-misses');
        if (cancelled || !res.ok) return;
        const body = (await res.json()) as NearMissInsights;
        if (cancelled || !Array.isArray(body.targets)) return;
        setData(body);
      } catch {
        // Advisory card — swallow and stay hidden.
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (loading) {
    return (
      <Card aria-busy>
        <CardHeader>
          <CardTitle as='h2'>Almost Matched</CardTitle>
        </CardHeader>
        <CardContent>
          <Skeleton variant='text' lines={3} />
        </CardContent>
      </Card>
    );
  }

  const withTitles = data?.targets.filter(t => t.titles.length > 0) ?? [];
  if (withTitles.length === 0) return null;

  return (
    <Card>
      <CardHeader>
        <div className='flex items-baseline gap-x-4 gap-y-1 flex-wrap'>
          <CardTitle as='h2'>Almost Matched</CardTitle>
          <Text variant='meta'>
            Titles filtered out in the last {data?.window_days} days where the
            match gate wasn&rsquo;t sure (% = how confident the rejection was).
            If some look right, this target&rsquo;s scope may be narrower than
            you intended.
          </Text>
        </div>
      </CardHeader>
      <CardContent>
        <div className='space-y-4'>
          {withTitles.map(target => (
            <div key={target.target_id}>
              <Text variant='caption' className='mb-2'>
                {target.label}
              </Text>
              <ul className='flex flex-wrap gap-2'>
                {target.titles.map(t => (
                  <li key={t.title}>
                    <Badge variant='default' size='sm'>
                      <span className='capitalize'>{t.title}</span>
                      <span
                        className='ml-1.5 text-text-secondary tabular-nums'
                        aria-label={`rejected at ${t.confidence}% confidence`}
                      >
                        {t.confidence}%
                      </span>
                    </Badge>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
