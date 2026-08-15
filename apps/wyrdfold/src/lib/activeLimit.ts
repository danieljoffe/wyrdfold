/**
 * The structured half of the `ACTIVE_LIMIT` 409.
 *
 * `extractApiError` already turns this response into a sentence, which is the
 * right fallback. But the sentence is a dead end: it tells the user to
 * "deactivate one first" without saying which targets are holding the cap or
 * offering to do it. The API sends `active_targets` precisely so the client
 * can offer a swap, so this reads the payload rather than the prose.
 *
 * Kept separate from `extractApiError` because that function's contract is
 * `Response -> string`, and widening it to sometimes return an object would
 * complicate every one of its callers for one case.
 */

export interface ActiveTargetChoice {
  id: string;
  label: string;
}

export interface ActiveLimitDetail {
  limit: number;
  activeCount: number;
  message: string;
  /** Empty when the server couldn't resolve them — callers must handle that. */
  activeTargets: ActiveTargetChoice[];
}

function isChoice(v: unknown): v is ActiveTargetChoice {
  return (
    !!v &&
    typeof v === 'object' &&
    typeof (v as ActiveTargetChoice).id === 'string' &&
    typeof (v as ActiveTargetChoice).label === 'string'
  );
}

/**
 * Parse an `ACTIVE_LIMIT` 409 body, or `null` if this isn't one.
 *
 * Reads via `.clone()` so the caller can still hand the same response to
 * `extractApiError` for the fallback message.
 */
export async function parseActiveLimit(
  res: Response
): Promise<ActiveLimitDetail | null> {
  if (res.status !== 409) return null;
  let body: unknown;
  try {
    body = await res.clone().json();
  } catch {
    return null;
  }
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (
    !detail ||
    typeof detail !== 'object' ||
    (detail as { error?: unknown }).error !== 'ACTIVE_LIMIT'
  ) {
    return null;
  }
  const d = detail as {
    limit?: unknown;
    active_count?: unknown;
    message?: unknown;
    active_targets?: unknown;
  };
  return {
    limit: typeof d.limit === 'number' ? d.limit : 0,
    activeCount: typeof d.active_count === 'number' ? d.active_count : 0,
    message:
      typeof d.message === 'string' && d.message.trim()
        ? d.message
        : 'You are at your active-target limit.',
    activeTargets: Array.isArray(d.active_targets)
      ? d.active_targets.filter(isChoice)
      : [],
  };
}
