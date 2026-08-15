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

/**
 * What `/jobs` should do about a `?target=` it can't render a tab for.
 *
 * Two very different situations used to share one silent `redirect('/jobs')`:
 *
 *   - `'redirect'` — no membership for this caller. Still the right answer:
 *     an empty list would be worse, and acknowledging an id the caller has no
 *     membership for would confirm it exists.
 *   - `'paused'` — a real membership that is deactivated. The tab is omitted
 *     on purpose (a paused target has no rows here), but the request was
 *     well-formed and the answer is knowable. Silently dropping it landed the
 *     user on whichever OTHER target happened to be active — asking for X and
 *     being handed Y without a word.
 *   - `'ok'` — active membership, or no `?target=` at all.
 *
 * Split out of the page component so the branch is unit-testable without
 * rendering a server component.
 */
export type TargetResolution =
  { kind: 'ok' } | { kind: 'redirect' } | { kind: 'paused'; target: TargetTab };

export function resolveRequestedTarget(
  targetId: string | undefined,
  userTargets: UserTargetWithSummary[]
): TargetResolution {
  if (!targetId) return { kind: 'ok' };
  const membership = userTargets.find(t => t.target.id === targetId);
  if (!membership) return { kind: 'redirect' };
  if (membership.user_target.is_active) return { kind: 'ok' };
  return {
    kind: 'paused',
    target: { id: membership.target.id, label: membership.target.label },
  };
}
