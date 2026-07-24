/**
 * The one create → link → activate client pipeline (UX/IA §D).
 *
 * Target creation has several entry points — the targets page modal
 * (from-manual / from-url), experience + lateral suggestions, and the
 * onboarding wizard (generic create / from-posting) — and each used to
 * hand-roll the same fetch + error-extraction + response-projection
 * steps. These atoms are that pipeline, written once:
 *
 *   - ``createOrLinkTarget`` — the LLM-backed create-or-link endpoints,
 *     which link the caller server-side and return the full result.
 *   - ``createBareTarget`` — the onboarding wizard's plain create; used
 *     for suggestions that already went through LLM matching (re-running
 *     from-manual would repeat the match call ``/targets/suggest`` just
 *     made), so the wizard links + activates explicitly afterwards.
 *   - ``linkTarget`` / ``linkExistingTarget`` — attach the current user
 *     to a catalog target (raw row vs. list-entry projection).
 *   - ``activateTargetInBackground`` — fire-and-forget kick of the
 *     derive → poll → ready pipeline; failures surface as a still-idle
 *     target rather than a blocked flow, and the user can re-activate
 *     from /targets.
 *
 * Behavioral note (deliberately preserved, not unified): the targets
 * page does NOT activate after linking an existing target — activation
 * polls sources and spends crawl budget, and a catalog target a user
 * merely links is usually already live. The onboarding wizard DOES
 * activate (its targets are often brand new and would otherwise sit
 * idle with zero jobs).
 */

import { extractApiError } from '@/lib/extractApiError';
import type {
  CreateOrLinkResult,
  JobTarget,
  MatchedSuggestion,
  UserTarget,
  UserTargetWithSummary,
} from './types';
import { toSummary } from './types';

export type CreateOrLinkEndpoint =
  | '/api/targets/from-manual'
  | '/api/targets/from-url'
  | '/api/targets/from-suggestion';

/** POST one of the LLM-backed create-or-link endpoints. */
export async function createOrLinkTarget(
  endpoint: CreateOrLinkEndpoint,
  body: object,
  failureTitle = 'Failed to add target'
): Promise<CreateOrLinkResult> {
  const res = await fetch(endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    throw new Error(await extractApiError(res, failureTitle));
  }
  return (await res.json()) as CreateOrLinkResult;
}

export interface AddToTargetResult {
  success: boolean;
  job_posting_id: string;
  target_id: string;
  score: number;
}

/**
 * Add an EXISTING posting (a search result's job id) to one of the caller's
 * targets (#467 power-action). Scores the already-ingested posting against the
 * chosen target and saves it to the pipeline — no LLM, no new job row (unlike
 * {@link createOrLinkTarget}, which derives a whole target from a URL).
 */
export async function addJobToTarget(
  jobId: string,
  targetId: string
): Promise<AddToTargetResult> {
  const res = await fetch(`/api/jobs/${jobId}/add-to-target`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ target_id: targetId }),
  });
  if (!res.ok) {
    throw new Error(await extractApiError(res, 'Couldn’t add to target'));
  }
  return (await res.json()) as AddToTargetResult;
}

/** Project a create-or-link response to the targets-list entry shape (#863). */
export function toListEntry(result: CreateOrLinkResult): UserTargetWithSummary {
  return {
    user_target: result.user_target,
    target: toSummary(result.target),
  };
}

/** Bare row create (no LLM, no link) — the onboarding wizard's shape. */
export async function createBareTarget(body: {
  label: string;
  description?: string;
}): Promise<JobTarget> {
  const res = await fetch('/api/targets', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    throw new Error(await extractApiError(res, 'Failed to create target'));
  }
  return (await res.json()) as JobTarget;
}

/** Attach the current user to a catalog target; returns the raw link row. */
export async function linkTarget(targetId: string): Promise<UserTarget> {
  const res = await fetch(`/api/targets/${targetId}/link`, {
    method: 'POST',
  });
  if (!res.ok) {
    throw new Error(await extractApiError(res, 'Link failed'));
  }
  return (await res.json()) as UserTarget;
}

/** Link an existing target and project to the targets-list entry shape. */
export async function linkExistingTarget(
  target: JobTarget
): Promise<UserTargetWithSummary> {
  const userTarget = await linkTarget(target.id);
  return { user_target: userTarget, target: toSummary(target) };
}

/**
 * Add one experience/lateral suggestion: brand-new labels go through the
 * LLM create-or-link endpoint (which normalizes + links server-side);
 * catalog matches just link.
 */
export async function addSuggestionTarget(
  match: MatchedSuggestion
): Promise<UserTargetWithSummary> {
  if (match.is_new) {
    const result = await createOrLinkTarget('/api/targets/from-manual', {
      label: match.suggestion.label,
      description: match.suggestion.description,
    });
    return toListEntry(result);
  }
  return linkExistingTarget(match.matched_target!);
}

/**
 * Add one AI search-suggestion the user picked (the catalog-search LLM
 * fallback). A single call to `/api/targets/from-suggestion`, which
 * create-or-links server-side.
 *
 * Deliberately NOT the client-side create→link dance {@link addSuggestionTarget}
 * uses: the server dedups on the (already-canonical) label at write time, so a
 * stale client `is_new` — or a race — links the existing catalog row instead of
 * minting a duplicate. And unlike `/from-manual` it needs no experience profile
 * (the fallback is meant for brand-new users; the role query is the signal), so
 * `match.is_new` / `match.matched_target` are ignored here — the server decides.
 * Links `is_active=False`; the user activates from /targets when ready (matching
 * the manual/URL create flows on that page).
 */
export async function addSearchSuggestionTarget(
  match: MatchedSuggestion
): Promise<UserTargetWithSummary> {
  const result = await createOrLinkTarget(
    '/api/targets/from-suggestion',
    {
      label: match.suggestion.label,
      description: match.suggestion.description,
    },
    'Failed to add target'
  );
  return toListEntry(result);
}

/**
 * Kick the activation pipeline (derive scoring profile → poll sources →
 * mark ready) without awaiting. Swallows failures deliberately — a lost
 * kickoff surfaces as a still-idle target the user can re-activate from
 * /targets, which beats blocking or crashing the calling flow.
 */
export function activateTargetInBackground(targetId: string): void {
  void fetch(`/api/targets/${targetId}/activate`, { method: 'POST' }).catch(
    () => undefined
  );
}
