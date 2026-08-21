/**
 * @jest-environment node
 */
import { NextRequest } from 'next/server';
import { config, proxy } from './proxy';

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

  it('report-only style-src is ALIGNED with enforced (nonce-tightening measured as infeasible)', async () => {
    // Dropping style-src to a nonce flooded every user's console — inline styles
    // are pervasive (Next/Tailwind + per-component width/height/padding +
    // env(safe-area-inset)). With no report-uri sink those reports were pure
    // noise, and a nonce-only style-src would need ~every inline style refactored
    // for marginal gain (real XSS vectors are already blocked by the nonce'd
    // script-src + object-src 'none' + base-uri 'self'). So report-only keeps
    // 'unsafe-inline' to match enforced; it still earns its keep via the
    // script-src https: drop (below).
    const { reportOnly, enforced } = await cspFor('https://app.test/dashboard');
    expect(enforced).toContain("style-src 'self' 'unsafe-inline'");
    expect(reportOnly).toContain("style-src 'self' 'unsafe-inline'");
    expect(reportOnly).not.toMatch(/style-src 'self' 'nonce-/);
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

describe('proxy middleware: public legal pages', () => {
  const original: Record<string, string | undefined> = {};

  beforeEach(() => {
    original[URL_VAR] = process.env[URL_VAR];
    original[ANON_VAR] = process.env[ANON_VAR];
    setEnv(URL_VAR, 'https://proj.supabase.co');
    setEnv(ANON_VAR, 'anon-key');
    // Anonymous visitor — the case that matters for public legal pages.
    mockGetUser.mockResolvedValue({ data: { user: null } });
  });

  afterEach(() => {
    setEnv(URL_VAR, original[URL_VAR]);
    setEnv(ANON_VAR, original[ANON_VAR]);
    mockGetUser.mockReset();
  });

  it.each(['/terms', '/privacy'])(
    'serves %s to anonymous visitors without redirecting to /login',
    async path => {
      const res = await proxy(new NextRequest(`https://app.test${path}`));
      // A public page response carries no redirect Location...
      expect(res.headers.get('location')).toBeNull();
      // ...and still ships the enforced CSP like every other route.
      expect(res.headers.get(ENFORCED_HEADER)).toBeTruthy();
    }
  );

  it('still redirects a protected route (/dashboard) to /login when anonymous', async () => {
    // Contrast: the allowlist is exact, not a blanket open-up.
    const res = await proxy(new NextRequest('https://app.test/dashboard'));
    expect(res.headers.get('location')).toContain('/login');
  });
});

// The auth-adaptive public search surface (#467 §10). Its allowlist entry is
// the highest-risk part of this feature: a too-broad match would punch a hole in
// the whole (app)/* gate. These tests pin it shut — /search opens, everything
// adjacent stays gated.
describe('proxy middleware: public /search (auth-adaptive surface)', () => {
  const original: Record<string, string | undefined> = {};

  beforeEach(() => {
    original[URL_VAR] = process.env[URL_VAR];
    original[ANON_VAR] = process.env[ANON_VAR];
    setEnv(URL_VAR, 'https://proj.supabase.co');
    setEnv(ANON_VAR, 'anon-key');
    // Anonymous visitor — the case that matters for the public funnel.
    mockGetUser.mockResolvedValue({ data: { user: null } });
  });

  afterEach(() => {
    setEnv(URL_VAR, original[URL_VAR]);
    setEnv(ANON_VAR, original[ANON_VAR]);
    mockGetUser.mockReset();
  });

  it('serves /search to anonymous visitors without redirecting to /login', async () => {
    const res = await proxy(new NextRequest('https://app.test/search'));
    // A public page response carries no redirect Location...
    expect(res.headers.get('location')).toBeNull();
    // ...and still ships the enforced CSP like every other route.
    expect(res.headers.get(ENFORCED_HEADER)).toBeTruthy();
  });

  it('serves /search WITH a query string to anonymous visitors (no redirect)', async () => {
    // The URL-synced search state rides the querystring; the pathname match must
    // still open it.
    const res = await proxy(
      new NextRequest('https://app.test/search?q=frontend&posted_within=7')
    );
    expect(res.headers.get('location')).toBeNull();
  });

  it.each(['/jobs', '/settings', '/dashboard', '/targets'])(
    'STILL redirects the gated route %s to /login when anonymous (allowlist is targeted, not a hole)',
    async path => {
      const res = await proxy(new NextRequest(`https://app.test${path}`));
      const location = res.headers.get('location');
      expect(location).toContain('/login');
      // The intended destination is preserved so login can bounce back.
      expect(location).toContain(`next=${encodeURIComponent(path)}`);
    }
  );

  it('does NOT open a lookalike prefix like /search-abuse (exact match only)', async () => {
    // Guards against `startsWith('/search')` creep — a prefix match would open
    // arbitrary `/search…` routes. The exact check keeps the hole to /search.
    const res = await proxy(new NextRequest('https://app.test/search-abuse'));
    expect(res.headers.get('location')).toContain('/login');
  });

  // ---- Shareable listing URLs (#467 §11.2 fast-follow) --------------------
  // The widened allowlist admits UUID-SHAPED `/search/<id>` paths only. These
  // pin the shape gate shut: a valid-looking listing URL opens, every
  // adversarial variant still bounces to /login.

  const LISTING_ID = '123e4567-e89b-42d3-a456-426614174000';

  it('serves /search/<uuid> (a shared listing link) to anonymous visitors', async () => {
    const res = await proxy(
      new NextRequest(`https://app.test/search/${LISTING_ID}`)
    );
    expect(res.headers.get('location')).toBeNull();
    // ...and still ships the enforced CSP like every other route.
    expect(res.headers.get(ENFORCED_HEADER)).toBeTruthy();
  });

  it('serves an UPPERCASE-uuid detail path too (ids are case-insensitively UUID-shaped)', async () => {
    const res = await proxy(
      new NextRequest(`https://app.test/search/${LISTING_ID.toUpperCase()}`)
    );
    expect(res.headers.get('location')).toBeNull();
  });

  it.each([
    ['a junk segment', '/search/junk'],
    ['uppercase junk', '/search/NOT-A-UUID-ATALL'],
    ['a uuid with an extra segment', `/search/${LISTING_ID}/extra`],
    ['an empty segment', '/search//'],
    ['a trailing slash on a uuid', `/search/${LISTING_ID}/`],
    ['a uuid one hex short', '/search/123e4567-e89b-42d3-a456-42661417400'],
    [
      'a uuid with a non-hex char',
      '/search/123e4567-e89b-42d3-a456-42661417400g',
    ],
    ['a lookalike prefix carrying a uuid', `/searchx/${LISTING_ID}`],
  ])(
    'still redirects %s (%s) to /login — the hole is exactly UUID-shaped',
    async (_label, path) => {
      const res = await proxy(new NextRequest(`https://app.test${path}`));
      expect(res.headers.get('location')).toContain('/login');
    }
  );
});

// ---- what the matcher lets through without auth (#899) ---------------------
//
// Anything NOT bypassed is redirected to /login when signed out. For an image
// that means a BROKEN image, because an email client or a home-screen
// installer is never authenticated.
//
// Not hypothetical: /logo.png is referenced by all three Supabase auth email
// templates and was returning 307 -> /login in production, so the logo was
// broken in every invite and sign-in email we sent.

describe('proxy matcher', () => {
  // Next compiles `source` as a path-to-regexp pattern; the negative lookahead
  // inside is plain RegExp syntax, so testing it directly faithfully answers
  // "is this path bypassed".
  const matcher = new RegExp(`^${config.matcher[0].source}$`);
  const requiresAuth = (path: string) => matcher.test(path);

  it.each([
    '/logo.png', // every auth email
    '/logo.svg',
    '/android-chrome-192x192.png', // site.webmanifest points here, and IS public
    '/android-chrome-512x512.png',
    '/apple-touch-icon.png', // iOS add-to-home-screen
    '/mstile-150x150.png',
    '/wyrdfold-mark.svg',
    '/favicon.ico', // pre-existing bypasses, kept honest
    '/favicon-32x32.png',
    '/site.webmanifest',
    '/images/hero.png',
    '/_next/static/chunk.js',
  ])('serves %s without auth', path => {
    expect(requiresAuth(path)).toBe(false);
  });

  it.each([
    '/dashboard',
    '/jobs',
    '/settings',
    '/targets',
    '/onboarding',
    '/api/billing/account',
  ])('still guards %s', path => {
    expect(requiresAuth(path)).toBe(true);
  });

  it('does not bypass a route merely because it contains a dot', () => {
    // Guards against "fixing" this with a blanket file-extension rule, which
    // would quietly take real routes out of the auth check.
    expect(requiresAuth('/jobs/some.id/resume')).toBe(true);
  });
});
