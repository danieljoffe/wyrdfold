import { Spinner } from '@danieljoffe/shared-ui/Spinner';
import CircleBadge, {
  type CircleBadgeVariant,
  type CircleBadgeSize,
} from '@/components/CircleBadge';
import { fitScoreVariant } from '@/lib/fitScore';

interface ScoreBadgeProps {
  score: number;
  /** Override the default score→colour mapping (e.g. the dashboard's). */
  variant?: CircleBadgeVariant;
  size?: CircleBadgeSize;
  /** When scoring is in flight, render a spinner beside the chip. */
  scoringStatus?: string | null | undefined;
  /**
   * True when the row has no real Sonnet fit grade — `score` is only a keyword
   * placeholder. Authoritative from the API (derived from `fit_reasoning`); when
   * omitted, falls back to `scoring_status !== 'complete'`.
   */
  pending?: boolean | undefined;
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
  pending,
  title,
  className,
}: ScoreBadgeProps) {
  // A not-yet-graded row carries only a keyword placeholder, not a real fit
  // score (#47). Show a neutral symbol — never the placeholder number, which
  // would read as a graded fit score. `pending` (fit_reasoning-derived) is
  // authoritative; fall back to the scoring_status heuristic when it's absent.
  // scoring_status alone is unreliable — 'complete' is set on ungraded rows.
  const isPending =
    pending ?? (!!scoringStatus && scoringStatus !== 'complete');
  // Spinner only while actively scoring — not for a done-but-ungraded row.
  const isScoring = !!scoringStatus && scoringStatus !== 'complete';
  return (
    <span className='inline-flex shrink-0 items-center gap-1'>
      <CircleBadge
        variant={isPending ? 'default' : (variant ?? fitScoreVariant(score))}
        size={size}
        title={isPending ? 'Not yet scored — pending a full fit grade' : title}
        ariaLabel={isPending ? 'Fit score pending' : `Match score ${score}`}
        className={className}
      >
        {isPending ? '·' : score}
      </CircleBadge>
      {isPending && isScoring && (
        <Spinner
          size='sm'
          aria-label={`Scoring in progress (${scoringStatus})`}
        />
      )}
    </span>
  );
}
