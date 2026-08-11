import type { UserTargetWithSummary } from '../targets/types';
import type { TargetTab } from './JobsList';

/**
 * Map a user's target memberships to jobs-page tabs, OMITTING paused
 * (deactivated) memberships.
 *
 * Since the Group D schema decision (2026-07-30, docs/decisions.md) the /jobs
 * list scopes to ACTIVE memberships only, so a paused target's jobs never
 * appear — surfacing its tab would render a permanently empty list. Target
 * reactivation lives on /targets, not here. A deep link to a paused target
 * falls through to the "unknown target → redirect to /jobs" guard in page.tsx.
 */
export function toActiveTargetTabs(
  userTargets: UserTargetWithSummary[]
): TargetTab[] {
  return userTargets
    .filter(t => t.user_target.is_active)
    .map(t => ({ id: t.target.id, label: t.target.label }));
}
