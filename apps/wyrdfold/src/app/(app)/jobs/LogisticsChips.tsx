import { Badge } from '@danieljoffe/shared-ui/Badge';
import type { ComponentProps } from 'react';

import { cn } from '@/lib/cn';

import type { LogisticsFilters } from './types';

type BadgeVariant = NonNullable<ComponentProps<typeof Badge>['variant']>;

const REMOTE_LABEL: Record<'remote' | 'hybrid' | 'onsite', string> = {
  remote: 'Remote',
  hybrid: 'Hybrid',
  onsite: 'On-site',
};

// Remote is the desirable signal, so it gets the positive colour; hybrid is
// neutral-informational; on-site is plain. `unspecified` renders no chip.
const REMOTE_VARIANT: Record<'remote' | 'hybrid' | 'onsite', BadgeVariant> = {
  remote: 'success',
  hybrid: 'info',
  onsite: 'default',
};

function formatMoney(
  n: number,
  currency: string | null,
  unit: 'year' | 'hour' | null
): string {
  const sym = !currency || currency === 'USD' ? '$' : `${currency} `;
  // Hourly figures are small; annual figures render compactly as "$150k".
  return unit === 'hour' ? `${sym}${n}` : `${sym}${Math.round(n / 1000)}k`;
}

/** Explicit disclosed salary only (the grader nulls "competitive"/DOE). */
function formatSalary(f: LogisticsFilters): string | null {
  const { salary_min, salary_max, salary_currency, salary_unit } = f;
  if (salary_min == null && salary_max == null) return null;
  const suffix = salary_unit === 'hour' ? '/hr' : '';
  const money = (n: number) => formatMoney(n, salary_currency, salary_unit);
  if (salary_min != null && salary_max != null) {
    return salary_min === salary_max
      ? `${money(salary_min)}${suffix}`
      : `${money(salary_min)}–${money(salary_max)}${suffix}`;
  }
  if (salary_min != null) return `${money(salary_min)}+${suffix}`;
  return `Up to ${money(salary_max as number)}${suffix}`;
}

function formatLocation(f: LogisticsFilters): string | null {
  const { location_city, location_country } = f;
  if (location_city && location_country) {
    return `${location_city}, ${location_country}`;
  }
  return location_city || location_country || null;
}

interface LogisticsChipsProps {
  filters: LogisticsFilters | null | undefined;
  /** `compact` for list rows (tighter), `full` for the detail header. */
  variant?: 'compact' | 'full';
  className?: string;
}

/**
 * Inline logistics chips (#86) — remote status, salary, location — rendered from
 * the grader's `logistics_filters`. Pure presentation, filter-only data. Renders
 * nothing when there's no signal (all `unspecified`/null), so it's safe to drop
 * on every row: jobs not yet graded since extraction turned on simply show no
 * chips rather than an empty container.
 */
export default function LogisticsChips({
  filters,
  variant = 'compact',
  className,
}: LogisticsChipsProps) {
  if (!filters) return null;

  const remote =
    filters.remote_status && filters.remote_status !== 'unspecified'
      ? filters.remote_status
      : null;
  const salary = formatSalary(filters);
  const location = formatLocation(filters);

  if (!remote && !salary && !location) return null;

  return (
    <div
      className={cn(
        'flex flex-wrap items-center',
        variant === 'full' ? 'gap-2' : 'gap-1.5',
        className
      )}
      aria-label='Job logistics'
    >
      {remote && (
        <Badge variant={REMOTE_VARIANT[remote]} size='sm'>
          {REMOTE_LABEL[remote]}
        </Badge>
      )}
      {salary && (
        <Badge variant='default' size='sm'>
          {salary}
        </Badge>
      )}
      {location && (
        <Badge variant='default' size='sm'>
          {location}
        </Badge>
      )}
    </div>
  );
}
