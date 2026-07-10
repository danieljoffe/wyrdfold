import type { CircleBadgeVariant } from '@/components/CircleBadge';

/**
 * Canonical fit-score → semantic colour, shared by every surface that shows a
 * score (jobs list, dashboard, target cards). Green = strong match (≥70),
 * amber = partial (≥40), red = weak (<40). Single source of truth so a given
 * score is always the same colour everywhere — retune the thresholds here only.
 */
export function fitScoreVariant(score: number): CircleBadgeVariant {
  if (score >= 70) return 'success';
  if (score >= 40) return 'warning';
  return 'error';
}
