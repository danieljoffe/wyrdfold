/**
 * Turn a failed `POST /jobs/manual` response into copy a user can act on.
 *
 * The endpoint answers **200 with `success: false`** when it fetched something
 * but couldn't read a job posting out of it — only a malformed URL earns a 4xx.
 * Callers that branch on `res.ok` alone therefore report success for every
 * unreadable URL, which is exactly what shipped: pasting a LinkedIn link (the
 * most common thing to paste) toasted "Job added" and added nothing.
 *
 * The warning vocabulary comes from the API's own emitters —
 * `routers/jobs.py` (`http_status:{code}`, `redirect_domain_change:…`) and
 * `services/extract.py` (`firecrawl_failed:{reason}`). We map the ones a user
 * can do something about and fall back to a generic line for the rest, rather
 * than leaking raw tokens into the UI.
 */

/** HTTP statuses that mean "this host is refusing automated readers". */
const BLOCKED_STATUSES = new Set([401, 403, 429]);

function statusesIn(warnings: string[], prefix: string): number[] {
  return warnings
    .filter(w => w.startsWith(prefix))
    .map(w => Number.parseInt(w.slice(prefix.length).replace(/^http_/, ''), 10))
    .filter(n => Number.isFinite(n));
}

/**
 * Human-readable reason the add failed. `needsManualFields` callers should
 * pair this with the manual title/company/location form — the API returns
 * whatever it *did* extract so those fields can be pre-filled.
 */
export function describeAddJobFailure(warnings: string[]): string {
  const pageStatuses = statusesIn(warnings, 'http_status:');
  const crawlStatuses = statusesIn(warnings, 'firecrawl_failed:http_');
  const allStatuses = [...pageStatuses, ...crawlStatuses];

  if (allStatuses.some(s => BLOCKED_STATUSES.has(s))) {
    return "This site blocks automated readers, so we can't pull the posting from it. Open the job and paste the employer's own application link (Greenhouse, Lever, Ashby, Workday…) instead.";
  }
  if (allStatuses.includes(404)) {
    return 'That page returned 404 — the posting may have been taken down or the link may be incomplete.';
  }
  if (allStatuses.some(s => s >= 500)) {
    return 'That site returned a server error. It may be worth trying again in a few minutes.';
  }
  if (warnings.includes('too_many_redirects')) {
    return 'That link redirected too many times for us to follow.';
  }
  if (warnings.includes('content_verification:not_a_job_posting')) {
    return "That page doesn't look like a job posting.";
  }
  if (
    warnings.includes('firecrawl_failed:no_metadata') ||
    warnings.includes('firecrawl_failed:empty_html')
  ) {
    return "We reached the page but couldn't find a job posting on it. If it's behind a login or a search page, paste the direct posting URL.";
  }
  if (warnings.includes('fetch_failed')) {
    return "We couldn't reach that URL.";
  }
  return "We couldn't read a job posting from that URL.";
}
