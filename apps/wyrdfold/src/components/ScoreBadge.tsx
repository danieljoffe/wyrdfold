import { Spinner } from '@danieljoffe/shared-ui/Spinner';
import CircleBadge, {
  type CircleBadgeVariant,
  type CircleBadgeSize,
} from '@/components/CircleBadge';
import { fitScoreVariant } from '@/lib/fitScore';

/**
 * WHICH number this chip is showing. The app has two, on different scales,
 * and they are visible within two clicks of each other:
 *
 *   'fit'   — how well a TARGET suits YOUR EXPERIENCE (`user_targets.fit_score`)
 *   'match' — how well a JOB scores against a TARGET  (`scores.score`)
 *
 * They are not comparable, so they do not share a name. This chip used to
 * hardcode "Match score" into its accessible name while accepting a
 * caller-supplied tooltip, so on a target card a sighted user read "Fit score
 * 82" and a screen-reader user heard "Match score 82" off the same element.
 */
export type ScoreKind = 'fit' | 'match';

const SCORE_NOUN: Record<ScoreKind, string> = {
  fit: 'Fit score',
  match: 'Match score',
};

interface ScoreBadgeBaseProps {
  score: number;
  /**
   * Defaults to 'match': four of the five call sites are job chips, so the
   * common case is right without opting in, and the one target chip says so.
   */
  kind?: ScoreKind;
  /** Override the default score→colour mapping (e.g. the dashboard's). */
  variant?: CircleBadgeVariant;
  size?: CircleBadgeSize;
  /** Native tooltip on the chip (e.g. the fit-score reasoning). */
  title?: string | undefined;
  className?: string;
}

/**
 * `scoringStatus` may only be passed together with the API's `pending` flag.
 * The status-string fallback misreads terminal statuses (prod stamps
 * 'stage2' on fully-graded rows — issue #603, every /jobs row rendered as
 * an endless spinner), so a call site that has the status but drops the
 * flag is a bug this union makes unrepresentable.
 */
type ScoreBadgeProps = ScoreBadgeBaseProps &
  (
    | {
        /** When scoring is in flight, render a spinner beside the chip. */
        scoringStatus: string | null | undefined;
        /**
         * True when the row has no real Sonnet fit grade — `score` is only a
         * keyword placeholder. Authoritative from the API (derived from
         * `fit_reasoning`); when undefined, falls back to
         * `scoring_status !== 'complete'`.
         */
        pending: boolean | undefined;
      }
    | { scoringStatus?: undefined; pending?: boolean | undefined }
  );

/**
 * Circular match/fit score chip — a `CircleBadge` with the score-specific
 * colour mapping, accessible name, and an optional in-flight scoring spinner.
 */
export default function ScoreBadge({
  score,
  kind = 'match',
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
        title={
          isPending ? `Not yet scored — pending a full ${kind} grade` : title
        }
        ariaLabel={
          isPending
            ? `${SCORE_NOUN[kind]} pending`
            : `${SCORE_NOUN[kind]} ${score}`
        }
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
