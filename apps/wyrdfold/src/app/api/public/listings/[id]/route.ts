import { type NextRequest, NextResponse } from 'next/server';
import * as Sentry from '@sentry/nextjs';

import { bffSecretHeader } from '@/lib/api/bffSecret';
import { pickErrorMessage } from '@/lib/api/pickErrorMessage';
import { clientIp } from '@/lib/api/clientIp';

/**
 * Public (logged-out) single-listing read (#467 §11.2 fast-follow — shareable
 * `/search/<id>` URLs).
 *
 * A THIN BFF FORWARDER to wyrdfold-api's `GET /public/listings/{id}` — the
 * hard-load / deep-link counterpart to `/api/public/search`. Serves BOTH
 * audiences (the detail is public data; membership rides the separate authed
 * `/api/jobs/target-membership` call), so like the search forwarder it carries
 * no user credential.
 *
 * SECURITY POSTURE — clones `api/public/search/route.ts` exactly (do not
 * regress):
 *  - **NO Bearer.** No user credential at all — the unauth surface by
 *    construction.
 *  - **BFF shared secret** (`bffSecretHeader`) proves the call came through the
 *    trusted BFF, so a direct hit to Railway can't forge `X-Forwarded-For` to
 *    rotate past the backend's per-IP limit.
 *  - **Trusted client IP** (`clientIp` → Vercel `x-real-ip`) forwarded as
 *    `x-forwarded-for`; omitted entirely when we can't vouch for it.
 *  - The backend is the authoritative control surface: it types the id as a
 *    UUID (422 on junk), applies the live/US eligibility gate (404 without an
 *    existence leak), rate-limits, and returns the preview projection. We pass
 *    its status + body straight through (incl. 404 and 429 + Retry-After).
 *    The id is URL-encoded so a hostile segment can't restructure the upstream
 *    path.
 */

const GENERIC_ERROR = 'Something went wrong. Please try again.';

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;

  const baseUrl = process.env['WYRDFOLD_API_URL'];
  if (!baseUrl) {
    // Misconfiguration, not a client error — fail closed with a generic body.
    Sentry.captureMessage(
      'WYRDFOLD_API_URL not configured for /api/public/listings',
      { tags: { route: 'api/public/listings' } }
    );
    return NextResponse.json({ error: GENERIC_ERROR }, { status: 503 });
  }

  const ip = clientIp(request);
  try {
    const res = await fetch(
      `${baseUrl}/public/listings/${encodeURIComponent(id)}`,
      {
        method: 'GET',
        headers: {
          // NO Authorization header — this is the unauthenticated surface.
          // Prove the call came through the BFF (SEC-5) so the backend accepts
          // it and trusts the forwarded IP below.
          ...bffSecretHeader(),
          // Forward the TRUSTED client IP as `x-forwarded-for` so uvicorn
          // `--proxy-headers` + slowapi key the per-IP limit on the real
          // visitor. Omitted when we don't have a trustworthy IP.
          ...(ip ? { 'x-forwarded-for': ip } : {}),
        },
      }
    );

    const text = await res.text();
    let data: unknown = null;
    try {
      data = JSON.parse(text);
    } catch {
      // Non-JSON upstream body — handled per-branch below.
    }

    // Success: pass the listing through verbatim with the upstream status.
    if (res.ok) {
      if (data === null) {
        // OK but unparseable — treat as an upstream fault rather than shipping
        // raw text to the browser.
        Sentry.captureMessage('Non-JSON 2xx from /public/listings upstream', {
          tags: { route: 'api/public/listings' },
        });
        return NextResponse.json({ error: GENERIC_ERROR }, { status: 502 });
      }
      return NextResponse.json(data, { status: res.status });
    }

    // Non-2xx: relay a generic, body-shaped error (never raw upstream text).
    // Prefer the backend's JSON `detail`/`error` string when present, and
    // preserve `Retry-After` so a 429 stays actionable for the client. The
    // status passes through — a backend 404 (missing OR delisted) reaches the
    // detail routes as a 404 so they render "listing unavailable"/notFound.
    const message = pickErrorMessage(data, GENERIC_ERROR);
    const headers: Record<string, string> = {};
    const retryAfter = res.headers.get('retry-after');
    if (retryAfter) headers['Retry-After'] = retryAfter;
    return NextResponse.json(
      { error: message },
      { status: res.status, headers }
    );
  } catch (err) {
    Sentry.captureException(err, { tags: { route: 'api/public/listings' } });
    return NextResponse.json({ error: GENERIC_ERROR }, { status: 502 });
  }
}
