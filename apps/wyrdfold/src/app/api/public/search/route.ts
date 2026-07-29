import { type NextRequest, NextResponse } from 'next/server';
import * as Sentry from '@sentry/nextjs';

import { bffSecretHeader } from '@/lib/api/bffSecret';
import { clientIp } from '@/lib/api/clientIp';

/**
 * Public (logged-out) keyword job search (#467 §10).
 *
 * This route is a THIN BFF FORWARDER to wyrdfold-api's `GET /public/search`
 * (unauth, BFF-only, hard depth-capped). It is the growth-funnel front door:
 * anonymous visitors search the shared, model-free jobs corpus — no session, no
 * per-user data, no match score, no JD body.
 *
 * SECURITY POSTURE — clones the waitlist (`api/waitlist/route.ts`), the app's
 * other public forwarder (do not regress):
 *  - **NO Bearer.** Unlike the authed `/api/jobs/search` (which proxies the
 *    caller's Supabase JWT), this route carries no user credential at all — it
 *    is the logged-out surface by construction.
 *  - **BFF shared secret** (`bffSecretHeader`) proves the call came through the
 *    trusted BFF, so a direct hit to Railway can't forge `X-Forwarded-For` to
 *    rotate past the backend's per-IP limit. The API's `require_bff_secret`
 *    fails OPEN when unset, so setting `WYRDFOLD_BFF_SECRET` on BOTH Vercel and
 *    Railway is a launch gate for this scrape-sensitive route (design §10).
 *  - **Trusted client IP** (`clientIp` → Vercel `x-real-ip`) forwarded as
 *    `x-forwarded-for` so the backend's slowapi keys the per-IP limit on the
 *    real visitor; omitted entirely when we can't vouch for it (never a
 *    spoofable value).
 *  - The backend is the authoritative control surface: it re-caps `page_size` /
 *    `offset` (anti-enumeration), rate-limits, and returns a preview projection.
 *    We pass its status + body straight through (incl. 429 + Retry-After).
 */

const GENERIC_ERROR = 'Something went wrong. Please try again.';

// Forwarded verbatim to the API's `GET /public/search`. The backend re-validates
// and hard-caps every one — this list is just the allowlist of params we relay,
// so an anonymous caller can't smuggle extra query args to the upstream.
const FORWARDED_PARAMS = [
  'q',
  'page_size',
  'offset',
  'location',
  'posted_within_days',
  'salary_floor',
] as const;

export async function GET(request: NextRequest) {
  const sp = request.nextUrl.searchParams;

  // `q` is required — mirror the authed route and short-circuit an obviously
  // pointless round trip (the backend also 422s on a missing/empty query).
  const q = sp.get('q')?.trim();
  if (!q) {
    return NextResponse.json(
      { error: 'q query param required' },
      { status: 400 }
    );
  }

  // Relay only the known search params (allowlist), trimming `q`. The backend is
  // the source of truth for validation + the hard depth caps.
  const forwarded = new URLSearchParams({ q });
  for (const key of FORWARDED_PARAMS) {
    if (key === 'q') continue;
    const value = sp.get(key);
    if (value) forwarded.set(key, value);
  }

  const baseUrl = process.env['WYRDFOLD_API_URL'];
  if (!baseUrl) {
    // Misconfiguration, not a client error — fail closed with a generic body.
    Sentry.captureMessage(
      'WYRDFOLD_API_URL not configured for /api/public/search',
      { tags: { route: 'api/public/search' } }
    );
    return NextResponse.json({ error: GENERIC_ERROR }, { status: 503 });
  }

  const ip = clientIp(request);
  try {
    const res = await fetch(`${baseUrl}/public/search?${forwarded.toString()}`, {
      method: 'GET',
      headers: {
        // NO Authorization header — this is the unauthenticated surface.
        // Prove the call came through the BFF (SEC-5) so the backend accepts it
        // and trusts the forwarded IP below.
        ...bffSecretHeader(),
        // Forward the TRUSTED client IP as `x-forwarded-for` so uvicorn
        // `--proxy-headers` + slowapi key the per-IP limit on the real visitor.
        // Omitted when we don't have a trustworthy IP (never spoofable).
        ...(ip ? { 'x-forwarded-for': ip } : {}),
      },
    });

    const text = await res.text();
    let data: unknown = null;
    try {
      data = JSON.parse(text);
    } catch {
      // Non-JSON upstream body — handled per-branch below.
    }

    // Success: pass the search results through verbatim with the upstream status.
    if (res.ok) {
      if (data === null) {
        // OK but unparseable — treat as an upstream fault rather than shipping
        // raw text to the browser.
        Sentry.captureMessage(
          'Non-JSON 2xx from /public/search upstream',
          { tags: { route: 'api/public/search' } }
        );
        return NextResponse.json({ error: GENERIC_ERROR }, { status: 502 });
      }
      return NextResponse.json(data, { status: res.status });
    }

    // Non-2xx: relay a generic, body-shaped error (never raw upstream text).
    // Prefer the backend's JSON `detail`/`error` string when present, and
    // preserve `Retry-After` so a 429 stays actionable for the client.
    let message = GENERIC_ERROR;
    if (data && typeof data === 'object') {
      const { detail, error } = data as { detail?: unknown; error?: unknown };
      if (typeof detail === 'string') message = detail;
      else if (typeof error === 'string') message = error;
    }
    const headers: Record<string, string> = {};
    const retryAfter = res.headers.get('retry-after');
    if (retryAfter) headers['Retry-After'] = retryAfter;
    return NextResponse.json(
      { error: message },
      { status: res.status, headers }
    );
  } catch (err) {
    Sentry.captureException(err, { tags: { route: 'api/public/search' } });
    return NextResponse.json({ error: GENERIC_ERROR }, { status: 502 });
  }
}
