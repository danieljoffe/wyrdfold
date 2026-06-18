import type { ComponentProps } from 'react';
import { Badge } from '@danieljoffe/shared-ui/Badge';
import { Spinner } from '@danieljoffe/shared-ui/Spinner';
import { cn } from '@/lib/cn';

type BadgeVariant = NonNullable<ComponentProps<typeof Badge>['variant']>;
type BadgeSize = NonNullable<ComponentProps<typeof Badge>['size']>;

// Fixed square dimensions per size so `rounded-full` yields a true circle at
// every score width (0–100). Comfortably fits three digits with tabular-nums.
const SIZE_CLASS: Record<BadgeSize, string> = {
  sm: 'size-7 text-[11px]',
  md: 'size-9 text-xs',
  lg: 'size-11 text-sm',
};

/** Default score → semantic colour, shared by the jobs surfaces. */
function defaultVariant(score: number): BadgeVariant {
  return score >= 70 ? 'success' : score >= 40 ? 'warning' : 'error';
}

interface ScoreBadgeProps {
  score: number;
  /** Override the default score→colour mapping (e.g. the dashboard's). */
  variant?: BadgeVariant;
  size?: BadgeSize;
  /** When scoring is in flight, render a spinner beside the chip. */
  scoringStatus?: string | null | undefined;
  /** Native tooltip on the chip (e.g. the fit-score reasoning). */
  title?: string | undefined;
  className?: string;
}

/**
 * Circular score chip.
 *
 * The shared-ui `Badge` is a content-width pill (`rounded-md` + horizontal
 * padding), so a two/three-digit score rendered as a lumpy oval. `Badge` runs
 * its className through tailwind-merge, so these classes cleanly override the
 * pill defaults — a fixed square + `rounded-full` + centred content + no
 * horizontal padding — to give a true circle while keeping the variant colours.
 */
export default function ScoreBadge({
  score,
  variant,
  size = 'md',
  scoringStatus,
  title,
  className,
}: ScoreBadgeProps) {
  const isScoring = !!scoringStatus && scoringStatus !== 'complete';
  return (
    <span className='inline-flex shrink-0 items-center gap-1'>
      <Badge
        variant={variant ?? defaultVariant(score)}
        size={size}
        aria-label={`Match score ${score}`}
        title={title}
        className={cn(
          'aspect-square justify-center rounded-full p-0 tabular-nums',
          SIZE_CLASS[size],
          className
        )}
      >
        {score}
      </Badge>
      {isScoring && (
        <Spinner
          size='sm'
          aria-label={`Scoring in progress (${scoringStatus})`}
        />
      )}
    </span>
  );
}
