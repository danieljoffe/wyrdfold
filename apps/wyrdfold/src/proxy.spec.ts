/**
 * @jest-environment node
 */
import { NextRequest } from 'next/server';
import { proxy } from './proxy';

// `auth.getUser()` is the only thing the CSP tests need from the Supabase
// client — stub it so `proxy()` runs past the auth gate and reaches the header
// branches. `getUser` is overridable per-test via `mockGetUser`. The
// missing-config tests below never reach `createServerClient` (they return at
// the 503 guard first), so this mock doesn't perturb them.
const mockGetUser = jest.fn();
jest.mock('@supabase/ssr', () => ({
  createServerClient: () => ({
    auth: { getUser: () => mockGetUser() },
  }),
}));

const URL_VAR = 'NEXT_PUBLIC_SUPABASE_URL';
const ANON_VAR = 'NEXT_PUBLIC_SUPABASE_ANON_ID';
const RO_HEADER = 'Content-Security-Policy-Report-Only';
const ENFORCED_HEADER = 'Content-Security-Policy';

function setEnv(name: string, value: string | undefined): void {
  if (value === undefined) {
    delete process.env[name];
  } else {
    process.env[name] = value;
  }
}

describe('proxy middleware: missing Supabase configuration', () => {
  const original: Record<string, string | undefined> = {};

  beforeEach(() => {
    original[URL_VAR] = process.env[URL_VAR];
    original[ANON_VAR] = process.env[ANON_VAR];
    original['NODE_ENV'] = process.env.NODE_ENV;
  });

  afterEach(() => {
    setEnv(URL_VAR, original[URL_VAR]);
    setEnv(ANON_VAR, original[ANON_VAR]);
    setEnv('NODE_ENV', original['NODE_ENV']);
  });

  it('returns 503 (not 401) when both Supabase vars are absent', async () => {
    setEnv(URL_VAR, undefined);
    setEnv(ANON_VAR, undefined);

    const res = await proxy(new NextRequest('http://localhost:3100/dashboard'));

    expect(res.status).toBe(503);
  });

  it('names every missing var and the remedy in development', async () => {
    setEnv(URL_VAR, undefined);
    setEnv(ANON_VAR, undefined);

    const res = await proxy(new NextRequest('http://localhost:3100/dashboard'));
    const body = await res.text();

    expect(body).toContain(URL_VAR);
    expect(body).toContain(ANON_VAR);
    expect(body).toContain('.env.local');
  });

  it('lists only the var that is actually missing', async () => {
    setEnv(URL_VAR, 'http://127.0.0.1:54321');
    setEnv(ANON_VAR, undefined);

    const res = await proxy(new NextRequest('http://localhost:3100/dashboard'));
    const body = await res.text();

    expect(body).toContain(ANON_VAR);
    expect(body).not.toContain(URL_VAR);
  });

  it('stays terse in production so a misconfigured deploy leaks nothing', async () => {
    setEnv(URL_VAR, undefined);
    setEnv(ANON_VAR, undefined);
    setEnv('NODE_ENV', 'production');

    const res = await proxy(new NextRequest('http://localhost:3100/dashboard'));
    const body = await res.text();

    expect(res.status).toBe(503);
    expect(body).not.toContain(URL_VAR);
    expect(body).not.toContain(ANON_VAR);
  });
});

describe('proxy middleware: Content-Security-Policy-Report-Only (audit #29 M1)', () => {
  const original: Record<string, string | undefined> = {};

  beforeEach(() => {
    original[URL_VAR] = process.env[URL_VAR];
    original[ANON_VAR] = process.env[ANON_VAR];
    original['NODE_ENV'] = process.env.NODE_ENV;
    // Valid config so proxy() builds + sets the CSP headers (no 503 short-circuit).
    setEnv(URL_VAR, 'https://proj.supabase.co');
    setEnv(ANON_VAR, 'anon-key');
    // Authenticated so a protected route falls through to the final header set
    // instead of redirecting to /login.
    mockGetUser.mockResolvedValue({ data: { user: { id: 'u1' } } });
  });

  afterEach(() => {
    setEnv(URL_VAR, original[URL_VAR]);
    setEnv(ANON_VAR, original[ANON_VAR]);
    setEnv('NODE_ENV', original['NODE_ENV']);
    mockGetUser.mockReset();
  });

  async function cspFor(url: string): Promise<{
    reportOnly: string | null;
    enforced: string | null;
  }> {
    const res = await proxy(new NextRequest(url));
    return {
      reportOnly: res.headers.get(RO_HEADER),
      enforced: res.headers.get(ENFORCED_HEADER),
    };
  }

  it('sets a Report-Only header on an authenticated document response', async () => {
    const { reportOnly } = await cspFor('https://app.test/dashboard');
    expect(reportOnly).toBeTruthy();
  });

  it('is REPORT-ONLY, never the enforcing header value — they are distinct', async () => {
    // The enforcing header must still be present (existing behaviour) AND the
    // report-only header must be a *different*, stricter policy. If they were
    // identical the report-only header would add no measurement signal.
    const { reportOnly, enforced } = await cspFor('https://app.test/dashboard');
    expect(enforced).toBeTruthy();
    expect(reportOnly).toBeTruthy();
    expect(reportOnly).not.toBe(enforced);
  });

  it('carries the core strict directives the audit asked for', async () => {
    const { reportOnly } = await cspFor('https://app.test/dashboard');
    expect(reportOnly).toContain("default-src 'self'");
    expect(reportOnly).toContain("object-src 'none'");
    expect(reportOnly).toContain("base-uri 'self'");
    expect(reportOnly).toContain("form-action 'self'");
    expect(reportOnly).toContain("frame-ancestors 'none'");
    expect(reportOnly).toMatch(/script-src [^;]*'nonce-[^']+'/);
  });

  it('is STRICTER than enforced: report-only style-src drops unsafe-inline for a nonce', async () => {
    // This is the whole point of the report-only policy — measure what removing
    // `'unsafe-inline'` from style-src would break before enforcing it.
    const { reportOnly, enforced } = await cspFor('https://app.test/dashboard');
    expect(enforced).toContain("style-src 'self' 'unsafe-inline'");
    expect(reportOnly).toMatch(/style-src 'self' 'nonce-[^']+'/);
    expect(reportOnly).not.toContain("style-src 'self' 'unsafe-inline'");
  });

  it('is STRICTER than enforced: report-only script-src drops the https: host fallback', async () => {
    const { reportOnly, enforced } = await cspFor('https://app.test/dashboard');
    // Enforced keeps the (strict-dynamic-neutered) https: fallback; report-only does not.
    expect(enforced).toMatch(/script-src 'self'[^;]* https:/);
    expect(reportOnly).not.toMatch(/script-src 'self'[^;]* https:/);
  });

  it('reuses the per-request nonce so enforced + report-only agree on it', async () => {
    const { reportOnly, enforced } = await cspFor('https://app.test/dashboard');
    const nonceOf = (csp: string | null) => csp?.match(/'nonce-([^']+)'/)?.[1];
    const ro = nonceOf(reportOnly);
    expect(ro).toBeTruthy();
    expect(nonceOf(enforced)).toBe(ro);
  });

  it('keeps Supabase + Sentry origins reachable (report-only must not over-tighten connect-src)', async () => {
    const { reportOnly } = await cspFor('https://app.test/dashboard');
    expect(reportOnly).toContain("connect-src 'self'");
    expect(reportOnly).toContain('https://*.supabase.co');
    expect(reportOnly).toContain('https://*.sentry.io');
    // The env-derived Supabase origin is also allowed.
    expect(reportOnly).toContain('https://proj.supabase.co');
  });

  it('sets the Report-Only header on /api/* responses too', async () => {
    const { reportOnly } = await cspFor('https://app.test/api/jobs');
    expect(reportOnly).toBeTruthy();
    expect(reportOnly).toContain("default-src 'self'");
  });

  it('sets the Report-Only header on the public landing page for anonymous users', async () => {
    mockGetUser.mockResolvedValue({ data: { user: null } });
    const { reportOnly } = await cspFor('https://app.test/');
    expect(reportOnly).toBeTruthy();
  });
});
