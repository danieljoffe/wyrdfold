import { timingSafeEqual } from 'node:crypto';
import { type NextRequest, NextResponse } from 'next/server';

function constantTimeEqual(a: string, b: string): boolean {
  const aBuf = Buffer.from(a, 'utf8');
  const bBuf = Buffer.from(b, 'utf8');
  if (aBuf.length !== bBuf.length) return false;
  return timingSafeEqual(aBuf, bBuf);
}

/**
 * Cron-only global poll trigger.
 *
 * This route force-polls EVERY enabled source across ALL tenants via the
 * upstream API-key-gated `/poll` endpoint (RLS-bypassed, service-role).
 * It is a privileged, cost-bearing, all-tenant fan-out — the only legitimate
 * caller is the Vercel cron defined in `vercel.json`
 * (`{ path: "/api/jobs/poll", schedule: "0 9 * * *" }`), which presents the
 * `CRON_SECRET` as a Bearer token.
 *
 * It is NOT user-facing: nothing in the app calls it from a user action
 * (verified — no `/api/jobs/poll` references in the frontend). The
 * per-user "refresh now" surface is the separately authenticated,
 * ownership-checked `/api/targets/[id]/poll-jobs` route, which forwards the
 * user's JWT and polls only that target's sources.
 *
 * Previously this route also accepted any authenticated user session and
 * then forwarded with the shared `WYRDFOLD_API_KEY`, letting any logged-in
 * user trigger the global all-tenant poll (privilege escalation + abuse
 * vector, audit #29). The session path is removed: a request that does not
 * present the cron secret is rejected outright. Fails closed when
 * `CRON_SECRET` is unset so a misconfigured deploy can't be probed into the
 * privileged path.
 */
export async function POST(request: NextRequest) {
  const cronSecret = process.env['CRON_SECRET'];
  if (!cronSecret) {
    return NextResponse.json(
      { error: 'CRON_SECRET not configured' },
      { status: 503 }
    );
  }

  const authHeader = request.headers.get('authorization') ?? '';
  const presented = authHeader.startsWith('Bearer ')
    ? authHeader.slice('Bearer '.length)
    : '';
  if (!constantTimeEqual(presented, cronSecret)) {
    return NextResponse.json({ error: 'Forbidden' }, { status: 403 });
  }

  // Upstream /poll is operator-key-gated. The BFF holds the narrow,
  // operator-only WYRDFOLD_CRON_KEY (audit #29 — this cron path uses ONLY this
  // key, never the broad WYRDFOLD_API_KEY) and attaches it after the cron
  // secret is verified. Requires WYRDFOLD_CRON_KEY to be set in this app's env
  // and accepted by the API's /poll route.
  const apiKey = process.env['WYRDFOLD_CRON_KEY'];
  if (!apiKey) {
    return NextResponse.json(
      { error: 'WYRDFOLD_CRON_KEY not configured' },
      { status: 503 }
    );
  }

  const baseUrl = process.env['WYRDFOLD_API_URL'] ?? '';
  try {
    const res = await fetch(`${baseUrl}/poll`, {
      method: 'POST',
      headers: {
        'x-api-key': apiKey,
        'Content-Type': 'application/json',
      },
    });
    const text = await res.text();
    try {
      return NextResponse.json(JSON.parse(text), { status: res.status });
    } catch {
      return NextResponse.json(
        { error: 'Upstream returned non-JSON', upstreamStatus: res.status },
        { status: 502 }
      );
    }
  } catch (err) {
    const detail =
      process.env.NODE_ENV !== 'production' && err instanceof Error
        ? { message: err.message }
        : undefined;
    return NextResponse.json(
      {
        error: 'WyrdFold API unavailable',
        ...(detail ? { detail } : {}),
      },
      { status: 503 }
    );
  }
}
