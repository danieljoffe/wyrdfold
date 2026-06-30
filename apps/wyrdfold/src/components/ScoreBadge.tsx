import { Spinner } from '@danieljoffe/shared-ui/Spinner';
import CircleBadge, {
  type CircleBadgeVariant,
  type CircleBadgeSize,
} from '@/components/CircleBadge';

/** Default score → semantic colour, shared by the jobs surfaces. */
function defaultVariant(score: number): CircleBadgeVariant {
  return score >= 70 ? 'success' : score >= 40 ? 'warning' : 'error';
}

interface ScoreBadgeProps {
  score: number;
  /** Override the default score→colour mapping (e.g. the dashboard's). */
  variant?: CircleBadgeVariant;
  size?: CircleBadgeSize;
  /** When scoring is in flight, render a spinner beside the chip. */
  scoringStatus?: string | null | undefined;
  /** Native tooltip on the chip (e.g. the fit-score reasoning). */
  title?: string | undefined;
  className?: string;
}

/**
 * Circular match/fit score chip — a `CircleBadge` with the score-specific
 * colour mapping, accessible name, and an optional in-flight scoring spinner.
 */
export default function ScoreBadge({
  score,
  variant,
  size = 'md',
  scoringStatus,
  title,
  className,
}: ScoreBadgeProps) {
  // A not-yet-graded row carries only a keyword placeholder, not a real fit
  // score (#47). Show a neutral "pending" chip — never the placeholder number,
  // which would read as a graded fit score — plus the in-flight spinner. So the
  // number the user sees on a chip is always a real Sonnet grade.
  const isPending = !!scoringStatus && scoringStatus !== 'complete';
  return (
    <span className='inline-flex shrink-0 items-center gap-1'>
      <CircleBadge
        variant={isPending ? 'default' : (variant ?? defaultVariant(score))}
        size={size}
        title={isPending ? 'Not yet scored — pending a full fit grade' : title}
        ariaLabel={isPending ? 'Fit score pending' : `Match score ${score}`}
        className={className}
      >
        {isPending ? '·' : score}
      </CircleBadge>
      {isPending && (
        <Spinner
          size='sm'
          aria-label={`Scoring in progress (${scoringStatus})`}
        />
      )}
    </span>
  );
}
