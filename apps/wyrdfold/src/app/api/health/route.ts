import { NextResponse } from 'next/server';

// Public liveness endpoint. Returns 200 with a constant body as long as
// the Next.js process is serving — no auth, no upstream call, no DB.
// Self-hosters point UptimeRobot / BetterUptime / their LB health probe
// at this. The API has its own `/health` (FastAPI side) for the same
// purpose; both URLs should be monitored independently. See the
// Operations section of the README.

export const dynamic = 'force-dynamic';

export function GET() {
  return NextResponse.json({ status: 'ok' });
}
