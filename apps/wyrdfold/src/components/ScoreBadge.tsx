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
  const isScoring = !!scoringStatus && scoringStatus !== 'complete';
  return (
    <span className='inline-flex shrink-0 items-center gap-1'>
      <CircleBadge
        variant={variant ?? defaultVariant(score)}
        size={size}
        title={title}
        ariaLabel={`Match score ${score}`}
        className={className}
      >
        {score}
      </CircleBadge>
      {isScoring && (
        <Spinner
          size='sm'
          aria-label={`Scoring in progress (${scoringStatus})`}
        />
      )}
    </span>
  );
}
