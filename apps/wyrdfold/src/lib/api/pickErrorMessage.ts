/**
 * The BFF's server-side error-message ladder (#971 §2): a FastAPI-style
 * string `detail` beats a route-normalized string `error`, else the caller's
 * fallback. Previously copy-pasted across the waitlist, public-listings and
 * public-search routes (the last two line-for-line twins), so a precedence
 * or trimming change had to be found in three places.
 *
 * Only NON-BLANK strings win — the old twins let an empty-string `detail`
 * overwrite the generic message with "", a blank toast. (The client-side
 * `extractApiError` shares the precedence rule but not the shape: its
 * `error` check sits after five structured-`detail` branches, so it stays
 * separate by design.)
 */
export function pickErrorMessage(data: unknown, fallback: string): string {
  if (data && typeof data === 'object') {
    const { detail, error } = data as { detail?: unknown; error?: unknown };
    if (typeof detail === 'string' && detail.trim()) return detail;
    if (typeof error === 'string' && error.trim()) return error;
  }
  return fallback;
}
