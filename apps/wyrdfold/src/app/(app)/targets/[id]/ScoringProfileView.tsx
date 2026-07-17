'use client';

import { useMemo } from 'react';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@danieljoffe/shared-ui/Card';
import { Text } from '@danieljoffe/shared-ui/Text';
import CircleBadge from '@/components/CircleBadge';
import type { CategoryProfile, JobTarget, ScoringProfile } from '../types';
import { emptyScoringProfile } from '../types';

interface ScoringProfileViewProps {
  target: JobTarget;
}

function weightBadgeVariant(w: number): 'default' | 'info' | 'brand' {
  if (w === 1) return 'default';
  if (w === 2) return 'info';
  return 'brand';
}

/**
 * Read-only presentation of a target's SHARED scoring profile.
 *
 * The scoring model is shared by everyone who follows a target
 * (`find_matching_target` dedups targets by label globally), so it is not
 * editable per user — a direct edit would rewrite the rubric every co-searcher
 * depends on (SEC-2, #366). Users shape their own list via axis weights /
 * preferences (`user_targets`), and contribute to the shared model only
 * through the bounded #191 path (reference JDs + the learning log). This
 * component therefore renders the profile without any inputs or save afford.
 */
export default function ScoringProfileView({
  target,
}: ScoringProfileViewProps) {
  const profile: ScoringProfile =
    target.scoring_profile ?? emptyScoringProfile();

  const categoryEntries = useMemo(
    () => Object.entries(profile.categories) as [string, CategoryProfile][],
    [profile.categories]
  );

  const totalKeywords = useMemo(
    () =>
      categoryEntries.reduce(
        (sum, [, c]) => sum + Object.keys(c.keywords).length,
        0
      ),
    [categoryEntries]
  );

  const isEmpty =
    categoryEntries.length === 0 &&
    profile.seniority.signals.length === 0 &&
    !profile.seniority.level &&
    profile.domain.signals.length === 0 &&
    profile.negative.keywords.length === 0;

  if (isEmpty) {
    return (
      <Card>
        <CardContent className='py-8'>
          <Text variant='body' className='text-text-secondary'>
            This target doesn&rsquo;t have a scoring profile yet — it&rsquo;s
            derived automatically after activation.
          </Text>
        </CardContent>
      </Card>
    );
  }

  return (
    <div className='flex flex-col gap-4'>
      {/* ---- Categories ---- */}
      {categoryEntries.length > 0 && (
        <Card>
          <CardHeader>
            <div className='flex items-baseline justify-between gap-2'>
              <CardTitle>Categories</CardTitle>
              <Text variant='meta' className='text-text-tertiary'>
                {categoryEntries.length} categories · {totalKeywords} keywords
              </Text>
            </div>
            <Text variant='meta' className='text-text-secondary'>
              Keywords grouped by theme. Each keyword carries a 1–3 weight; the
              category weight scales the whole group.
            </Text>
          </CardHeader>
          <CardContent className='flex flex-col gap-3'>
            {categoryEntries.map(([catName, cat]) => (
              <div
                key={catName}
                className='rounded-lg border border-border p-4 flex flex-col gap-3'
              >
                <div className='flex items-center justify-between gap-2'>
                  <Text variant='label' as='span'>
                    {catName}
                  </Text>
                  <Text variant='meta' className='text-text-secondary'>
                    Weight {cat.weight}
                  </Text>
                </div>
                {Object.keys(cat.keywords).length > 0 && (
                  <div className='flex flex-wrap gap-2'>
                    {Object.entries(cat.keywords).map(([kw, weight]) => (
                      <span
                        key={kw}
                        className='flex items-center gap-1 rounded-full border border-border px-2.5 py-1 text-xs'
                      >
                        <span className='text-text-primary'>{kw}</span>
                        <CircleBadge
                          variant={weightBadgeVariant(weight)}
                          size='sm'
                          ariaLabel={`Weight ${weight}`}
                        >
                          {weight}
                        </CircleBadge>
                      </span>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </CardContent>
        </Card>
      )}

      {/* ---- Seniority ---- */}
      {(profile.seniority.level || profile.seniority.signals.length > 0) && (
        <Card>
          <CardHeader>
            <CardTitle>Seniority</CardTitle>
          </CardHeader>
          <CardContent className='flex flex-col gap-3'>
            {profile.seniority.level && (
              <Text variant='body' className='text-text-primary'>
                Level: {profile.seniority.level}
              </Text>
            )}
            <ReadOnlyTagList
              label='Signals'
              items={profile.seniority.signals}
            />
          </CardContent>
        </Card>
      )}

      {/* ---- Domain ---- */}
      {profile.domain.signals.length > 0 && (
        <Card>
          <CardHeader>
            <div className='flex items-baseline justify-between gap-2'>
              <CardTitle>Domain</CardTitle>
              <Text variant='meta' className='text-text-tertiary'>
                Weight {profile.domain.weight}
              </Text>
            </div>
          </CardHeader>
          <CardContent>
            <ReadOnlyTagList label='Signals' items={profile.domain.signals} />
          </CardContent>
        </Card>
      )}

      {/* ---- Penalties ---- */}
      {profile.negative.keywords.length > 0 && (
        <Card>
          <CardHeader>
            <div className='flex items-baseline justify-between gap-2'>
              <CardTitle>Penalties</CardTitle>
              <Text variant='meta' className='text-text-tertiary'>
                Weight {profile.negative.weight}
              </Text>
            </div>
          </CardHeader>
          <CardContent>
            <ReadOnlyTagList
              label='Keywords'
              items={profile.negative.keywords}
            />
          </CardContent>
        </Card>
      )}
    </div>
  );
}

// ---- Read-only tag list ----

interface ReadOnlyTagListProps {
  label: string;
  items: string[];
}

function ReadOnlyTagList({ label, items }: ReadOnlyTagListProps) {
  if (items.length === 0) return null;
  return (
    <div className='flex flex-col gap-2'>
      <Text variant='label' as='span'>
        {label}
      </Text>
      <div className='flex flex-wrap gap-1.5'>
        {items.map(item => (
          <span
            key={item}
            className='rounded-full bg-surface-secondary px-2.5 py-1 text-xs text-text-primary'
          >
            {item}
          </span>
        ))}
      </div>
    </div>
  );
}
