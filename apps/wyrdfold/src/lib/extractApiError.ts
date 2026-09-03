/**
 * Parse a failing ``Response`` into a human-readable error message.
 *
 * Handles three FastAPI error shapes the wyrdfold-api emits:
 *
 *   1. ``{ detail: "..." }`` — plain string. The default
 *      ``HTTPException(detail=...)`` shape; surface verbatim.
 *
 *   2. ``{ detail: { code: 'llm_budget_exceeded', scope, limit_usd,
 *      spent_usd } }`` — structured budget-cap rejection (429). The
 *      previous error surface threw away the detail because the
 *      handler only checked ``typeof detail === 'string'`` — users
 *      saw a generic "Analysis failed (429)" with no recovery path.
 *      Format both the scope (hourly / daily) and the spend so the
 *      message is actionable.
 *
 *   3. Anything else (HTML, malformed JSON, no body, structured
 *      detail we don't recognize) — return ``fallback`` so the
 *      caller still has *something* to surface. Includes the HTTP
 *      status code in the fallback for debuggability.
 *
 * Reads the body via ``.clone()`` so the caller is free to read the
 * body again afterward (e.g. to extract a structured payload on
 * specific status codes).
 */
export async function extractApiError(
  res: Response,
  fallback: string
): Promise<string> {
  const statusFallback = `${fallback} (${res.status})`;
  let body: unknown;
  try {
    body = await res.clone().json();
  } catch {
    return statusFallback;
  }
  if (!body || typeof body !== 'object') return statusFallback;
  const detail = (body as { detail?: unknown }).detail;

  if (typeof detail === 'string' && detail.trim()) return detail.trim();

  // FastAPI pydantic validation errors arrive as
  // ``detail: [{ loc, msg, type, ... }, ...]``. Surface the first
  // entry's ``msg`` (stripping the ``Value error,`` prefix pydantic
  // adds when our validators raise ``ValueError``) so the user sees
  // "Phone must be E.164" instead of a generic fallback. SettingsPage
  // previously had a copy of this branch in its own
  // ``extractFastApiError``; centralizing here so every PATCH/PUT
  // gets the same treatment.
  //
  // Client-errors only (``status < 500``). A pydantic ``ValidationError``
  // array on a 5xx means the *server* failed to validate its own
  // response/payload — a server bug, not a user-actionable field error.
  // Surfacing it leaks raw pydantic text (e.g. the analysis 500 dumping
  // "scorecard Field required" into the UI), so for 5xx we fall through
  // to the generic ``statusFallback`` instead.
  if (res.status < 500 && Array.isArray(detail) && detail.length > 0) {
    const first = detail[0] as { msg?: unknown } | undefined;
    if (first && typeof first.msg === 'string' && first.msg.trim()) {
      return first.msg.replace(/^Value error,\s*/, '');
    }
  }

  if (
    detail &&
    typeof detail === 'object' &&
    'code' in detail &&
    (detail as { code: unknown }).code === 'llm_budget_exceeded'
  ) {
    const d = detail as {
      code: string;
      scope?: string;
      limit_usd?: number;
      spent_usd?: number;
    };
    const scope =
      d.scope === 'monthly'
        ? 'monthly'
        : d.scope === 'daily'
          ? 'daily'
          : 'hourly';
    const spent =
      typeof d.spent_usd === 'number' ? `$${d.spent_usd.toFixed(2)}` : null;
    const limit =
      typeof d.limit_usd === 'number' ? `$${d.limit_usd.toFixed(2)}` : null;
    const waitHint =
      scope === 'hourly'
        ? ' — try again in an hour'
        : scope === 'daily'
          ? ' — try again tomorrow'
          : ' — frees up as usage rolls out of the 30-day window';
    const label =
      scope === 'monthly' ? 'Monthly LLM allowance' : `LLM ${scope} budget`;
    if (spent && limit) {
      return `${label} reached (${spent} of ${limit})${waitHint}.`;
    }
    return `${label} reached${waitHint}.`;
  }

  if (
    detail &&
    typeof detail === 'object' &&
    'code' in detail &&
    (detail as { code: unknown }).code === 'llm_disabled'
  ) {
    return 'AI features are currently disabled for your account.';
  }

  if (
    detail &&
    typeof detail === 'object' &&
    'code' in detail &&
    (detail as { code: unknown }).code === 'analysis_daily_limit'
  ) {
    const d = detail as { limit?: number };
    const limit = typeof d.limit === 'number' ? ` (${d.limit}/day)` : '';
    return `Daily deep-analysis limit reached${limit} — more tomorrow. Already-analyzed jobs stay free to revisit.`;
  }

  // ``no_profile`` (404) — the user hasn't built their experience profile, so
  // analysis/tailoring can't run yet. Surface the backend's user-facing
  // ``message`` (never the raw "…POST /experience/derive first." path the
  // route used to leak, #105). Callers that want a CTA instead of a flat
  // error can branch on the code before reaching here.
  if (
    detail &&
    typeof detail === 'object' &&
    'code' in detail &&
    (detail as { code: unknown }).code === 'no_profile'
  ) {
    const d = detail as { message?: unknown };
    return typeof d.message === 'string' && d.message.trim()
      ? d.message
      : 'Set up your experience profile first.';
  }

  // ``ACTIVE_LIMIT`` (409) — the user is at their active-target cap
  // (``MAX_ACTIVE_TARGETS_PER_USER``, 1 by default). Raised from three
  // places: activate, follow/link, and add-from-posting.
  //
  // This branch keys on ``error``, not ``code``: the API composed this
  // payload with an ``error`` key while every other structured detail above
  // uses ``code``, so none of them matched and a ready-made, actionable
  // sentence was being dropped one layer from the screen — the user saw
  // "Activate failed (409)" and had no idea a cap even existed. The API
  // comment beside the raise site describes a frontend that reads this and
  // offers a deactivate picker; that picker doesn't exist yet, but surfacing
  // the server's own message is what it was meant to say.
  if (
    detail &&
    typeof detail === 'object' &&
    'error' in detail &&
    (detail as { error: unknown }).error === 'ACTIVE_LIMIT'
  ) {
    const d = detail as { message?: unknown; limit?: unknown };
    if (typeof d.message === 'string' && d.message.trim()) return d.message;
    // Server message missing/blank — say something true rather than falling
    // through to a bare status code.
    return typeof d.limit === 'number'
      ? `You can have ${d.limit} active target${d.limit === 1 ? '' : 's'} — deactivate one first.`
      : 'You are at your active-target limit — deactivate one first.';
  }

  // BFF proxy routes normalize every failure to a top-level
  // ``{ error: "..." }`` (e.g. /api/public/search) — a shape none of the
  // ``detail`` branches above can see, so public-search failures rendered
  // as bare status codes like "Search failed (422)" (#833). Checked last:
  // a FastAPI ``detail`` always wins when both keys are present.
  const error = (body as { error?: unknown }).error;
  if (typeof error === 'string' && error.trim()) return error.trim();

  return statusFallback;
}
