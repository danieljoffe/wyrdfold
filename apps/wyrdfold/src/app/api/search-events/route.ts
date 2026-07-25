import { type NextRequest, NextResponse } from 'next/server';

import { bffSecretHeader } from '@/lib/api/bffSecret';
import { clientIp } from '@/lib/api/clientIp';

/**
 * Search-funnel beacon (#467 §10 PR6) — forwards the two browser-only ticks
 * (`card_open`, `signup_click`) to wyrdfold-api's `POST /search-events`.
 *
 * SECURITY POSTURE — same as `api/public/search`, the sibling public
 * forwarder: NO Bearer (works logged-out; the ledger records no identity
 * anyway), BFF shared secret so the endpoint stays BFF-only, trusted
 * `clientIp` forwarded for the backend's per-IP brake. The payload is
 * re-validated strictly upstream (Literal event kinds, UUID id) — this route
 * only bounds the body size and relays.
 *
 * FIRE-AND-FORGET both ways: the browser never awaits this call's outcome
 * and we never surface analytics errors — a failed beacon costs one metric
 * row, nothing else. Always 204 to the caller (except an oversized body).
 */

// A beacon body is ~120 bytes; anything past 1 KiB is junk, not a beacon.
const MAX_BODY_BYTES = 1024;

export async function POST(request: NextRequest) {
  const raw = await request.text();
  if (raw.length > MAX_BODY_BYTES) {
    return new NextResponse(null, { status: 413 });
  }

  const baseUrl = process.env['WYRDFOLD_API_URL'];
  if (!baseUrl) {
    // Analytics is never worth an error page — swallow the misconfiguration
    // (the sibling data routes already alert on it) and drop the tick.
    return new NextResponse(null, { status: 204 });
  }

  const ip = clientIp(request);
  try {
    await fetch(`${baseUrl}/search-events`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        // NO Authorization header — the beacon is identity-free by design.
        ...bffSecretHeader(),
        ...(ip ? { 'x-forwarded-for': ip } : {}),
      },
      body: raw,
    });
  } catch {
    // Dropped tick — deliberately silent (see the module docstring).
  }
  return new NextResponse(null, { status: 204 });
}
