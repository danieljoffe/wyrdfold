import { type NextRequest, NextResponse } from 'next/server';
import * as Sentry from '@sentry/nextjs';

import { createServiceRoleClient } from '@/lib/supabase/admin-client';
import { clientIpFromHeaders, createRateLimiter } from '@/lib/rateLimit';

/**
 * Public waitlist signup (non-invited visitors on the marketing homepage).
 *
 * SECURITY (audit #29 — do not regress):
 *  - Writes go through the SERVICE-ROLE client into `waitlist_signups`, an
 *    RLS deny-all table. The browser NEVER touches the table directly.
 *  - Email is validated (shape + length cap) before any DB call.
 *  - Rate-limited per client IP to brake automated abuse (best-effort,
 *    per-instance — see lib/rateLimit.ts; the DB unique index is the hard
 *    idempotency backstop).
 *  - NO ENUMERATION: the response is a generic success whether the email is
 *    new, already on the list, or a duplicate race. We never reveal whether
 *    an address already exists.
 */

// Length cap matches the DB CHECK constraint (3..320). 320 is the practical
// RFC 5321 max (64 local + @ + 255 domain). Pragmatic, not pedantic.
const MAX_EMAIL_LENGTH = 320;
const MIN_EMAIL_LENGTH = 3;

// Single `@`, non-empty local part, a dot-bearing domain, no whitespace.
// Deliberately conservative — this is a gate against junk, not an RFC parser.
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

// 5 signups per IP per 10 minutes. Generous for a real human (who submits
// once), tight enough to make scripted list-stuffing pointless.
const rateLimiter = createRateLimiter({
  limit: 5,
  windowMs: 10 * 60 * 1000,
});

interface WaitlistBody {
  email?: unknown;
}

function isValidEmail(value: unknown): value is string {
  return (
    typeof value === 'string' &&
    value.length >= MIN_EMAIL_LENGTH &&
    value.length <= MAX_EMAIL_LENGTH &&
    EMAIL_RE.test(value)
  );
}

export async function POST(request: NextRequest) {
  const ip = clientIpFromHeaders(request.headers);
  const { allowed, resetAt } = rateLimiter.check(ip);
  if (!allowed) {
    const retryAfterS = Math.max(1, Math.ceil((resetAt - Date.now()) / 1000));
    return NextResponse.json(
      { error: 'Too many requests. Please try again later.' },
      { status: 429, headers: { 'Retry-After': String(retryAfterS) } }
    );
  }

  let body: WaitlistBody;
  try {
    body = (await request.json()) as WaitlistBody;
  } catch {
    return NextResponse.json(
      { error: 'Invalid request body.' },
      { status: 400 }
    );
  }

  const rawEmail =
    typeof body.email === 'string' ? body.email.trim() : body.email;
  if (!isValidEmail(rawEmail)) {
    return NextResponse.json(
      { error: 'Please enter a valid email address.' },
      { status: 400 }
    );
  }

  // Normalise to lower-case so the case-insensitive unique index de-dupes
  // and the stored value is canonical.
  const email = rawEmail.toLowerCase();

  try {
    const supabase = createServiceRoleClient();
    // `ignoreDuplicates` → ON CONFLICT DO NOTHING. A duplicate is NOT an
    // error here: it means the address is already on the list, which we
    // surface identically to a fresh signup (no enumeration).
    const { error } = await supabase
      .from('waitlist_signups')
      .upsert({ email }, { onConflict: 'email', ignoreDuplicates: true });

    if (error) {
      Sentry.captureException(error, {
        tags: { route: 'api/waitlist' },
      });
      return NextResponse.json(
        { error: 'Something went wrong. Please try again.' },
        { status: 500 }
      );
    }
  } catch (err) {
    Sentry.captureException(err, { tags: { route: 'api/waitlist' } });
    return NextResponse.json(
      { error: 'Something went wrong. Please try again.' },
      { status: 500 }
    );
  }

  // Generic success — identical for new and already-present emails.
  return NextResponse.json({ ok: true }, { status: 200 });
}
