import { NextRequest, NextResponse } from 'next/server';
import { createServerClient } from '@supabase/ssr';
import { allowedOrigins, allowedImageOrigins } from '@/utils/constants';
import { isProduction } from '@/utils/helpers';

// Default post-auth destination for signed-in users. The marketing landing
// page lives at `/`; authenticated users belong on the dashboard.
const HOME_DEFAULT = '/dashboard';

// Shareable listing URLs (#467 §11.2 fast-follow): `/search/<listing id>` must
// be reachable logged-out too — a shared link is the growth funnel's entry
// point. Listing ids are UUIDs, so the allowlist admits ONLY UUID-shaped detail
// paths: shape-restricting keeps the public hole to exactly the shareable
// surface. Anything else under /search (junk segments, extra path parts,
// trailing slashes) still falls through to the redirect-to-/login gate, so the
// widened entry can't become a wildcard hole into the (app)/* namespace.
const SEARCH_DETAIL_RE =
  /^\/search\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * Constrains `next` to a same-origin relative path. Anything else
 * (absolute URL, protocol-relative `//evil.com`, missing leading `/`,
 * or `/` itself which would just bounce back to the marketing page)
 * falls back to /dashboard so the redirect can't be abused.
 */
function safeNext(value: string | null): string {
  if (!value) return HOME_DEFAULT;
  if (!value.startsWith('/') || value.startsWith('//')) return HOME_DEFAULT;
  if (value === '/') return HOME_DEFAULT;
  return value;
}

function buildCspValue(
  request: NextRequest,
  nonce: string,
  extraConnectOrigins: string[] = []
): string {
  const cspHeader = `
    default-src 'self';
    script-src 'self' 'nonce-${nonce}' 'strict-dynamic' https: ${
      !isProduction() ? `'unsafe-eval'` : ''
    };
    style-src 'self' 'unsafe-inline';
    font-src 'self' https: data:;
    object-src 'none';
    base-uri 'self';
    form-action 'self';
    frame-ancestors 'none';${
      request.nextUrl.protocol === 'https:'
        ? `\n    upgrade-insecure-requests;`
        : ''
    }
    connect-src 'self' ${[...allowedOrigins, ...extraConnectOrigins].join(' ')};
    img-src 'self' blob: data: ${allowedImageOrigins.join(' ')};
`;
  return cspHeader.replace(/\s{2,}/g, ' ').trim();
}

// A *stricter* sibling of the enforced policy, shipped as
// `Content-Security-Policy-Report-Only` (audit #29, round 3 M1). Report-only is
// never enforced — the browser evaluates it and *reports* violations but blocks
// nothing — so it can't break the live app. Its job is to measure what a future
// *tightening of the enforced policy* would catch, before we make that change.
//
// The enforced `buildCspValue` above already covers the M1 ask (a real
// document-level CSP exists). It left two deliberately-loose spots to measure
// here — one is now RESOLVED:
//
//   1. `style-src 'unsafe-inline'` — MEASURED, and the report-only kept
//      `'unsafe-inline'` (i.e. aligned with enforced). Dropping it to a nonce
//      flooded every user's console: inline styles are pervasive (Next/Tailwind
//      + per-component width/height/padding + `env(safe-area-inset-*)`), so a
//      nonce-only `style-src` would require refactoring ~every inline style for
//      marginal gain (the real XSS vectors are already blocked by the nonce'd
//      `script-src` + `object-src 'none'` + `base-uri 'self'`). With no
//      report-uri sink the reports only clutter the console, so we do NOT
//      pursue this tightening. Left inline to document the decision.
//   2. `script-src … https:` — a host fallback that `'strict-dynamic'` already
//      neuters in supporting browsers. Still dropped here to confirm nothing
//      legitimately loads scripts from an arbitrary https host (this one is
//      quiet, so it stays under report-only observation).
//
// Reusing the same per-request `nonce` keeps Next's nonce-stamped
// <script>/<style> tags valid under both headers.
function buildReportOnlyCspValue(
  nonce: string,
  extraConnectOrigins: string[] = []
): string {
  const cspHeader = `
    default-src 'self';
    script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${
      !isProduction() ? ` 'unsafe-eval'` : ''
    };
    style-src 'self' 'unsafe-inline';
    font-src 'self' https: data:;
    object-src 'none';
    base-uri 'self';
    form-action 'self';
    frame-ancestors 'none';
    connect-src 'self' ${[...allowedOrigins, ...extraConnectOrigins].join(' ')};
    img-src 'self' blob: data: ${allowedImageOrigins.join(' ')};
`;
  return cspHeader.replace(/\s{2,}/g, ' ').trim();
}

export async function proxy(request: NextRequest) {
  const supabaseUrl = process.env['NEXT_PUBLIC_SUPABASE_URL'];
  const anonKey = process.env['NEXT_PUBLIC_SUPABASE_ANON_ID'];

  const nonce = Buffer.from(crypto.randomUUID()).toString('base64');
  // The browser-side Supabase client (auth `signOut`, token refresh) calls
  // `<supabaseUrl>/auth/v1/*` directly from the page, so the configured
  // Supabase origin must be in `connect-src`. Hosted prod is already covered
  // by the `*.supabase.co` wildcard in `allowedOrigins`, but local dev
  // (`http://127.0.0.1:54321`) and self-hosted / custom domains are not —
  // derive the origin from the env so logout works in every environment
  // instead of being silently blocked by CSP.
  const supabaseOrigin = (() => {
    try {
      return new URL(supabaseUrl ?? '').origin;
    } catch {
      return null;
    }
  })();
  const cspValue = buildCspValue(
    request,
    nonce,
    supabaseOrigin ? [supabaseOrigin] : []
  );
  // Non-enforcing companion policy (audit #29, round 3 M1). Set as
  // `Content-Security-Policy-Report-Only` below — it only reports, never blocks.
  const cspReportOnlyValue = buildReportOnlyCspValue(
    nonce,
    supabaseOrigin ? [supabaseOrigin] : []
  );

  if (!supabaseUrl || !anonKey) {
    // Missing anon URL/key is a server-side misconfiguration, not a failed
    // auth challenge — 503 is the honest status (no client credential fixes
    // it). In development we name the absent vars and the remedy; in
    // production we stay terse so a misconfigured deploy doesn't advertise
    // its internals to the public internet.
    const missing = [
      !supabaseUrl && 'NEXT_PUBLIC_SUPABASE_URL',
      !anonKey && 'NEXT_PUBLIC_SUPABASE_ANON_ID',
    ]
      .filter(Boolean)
      .join(', ');
    const body = isProduction()
      ? 'Service unavailable'
      : `Supabase configuration missing: ${missing}. Copy apps/wyrdfold/.env.example to apps/wyrdfold/.env.local and fill these in (see apps/wyrdfold/SETUP.md).`;
    return new NextResponse(body, { status: 503 });
  }

  // Forward the nonce + CSP on the *request* headers (not just the response)
  // so Next's renderer can read the nonce and stamp it onto every <script> it
  // emits — bootstrap, RSC flight, and chunk loaders. Without this the
  // `'strict-dynamic'` policy blocks every script, because that keyword
  // disables the `'self'`/`https:` host allow-list. It only takes effect when
  // the route renders per-request; the root layout reads `headers()` to force
  // that (a statically prerendered/CDN-cached page bakes its scripts with no
  // nonce). The browser-enforced CSP is still set on each response below.
  //
  // Rebuild from `request.headers` on each call (rather than mutating in
  // place) so the copy also picks up any auth cookies Supabase refreshes in
  // `setAll` — `request.cookies.set` writes through to the Cookie header.
  const forwardHeaders = () => {
    const requestHeaders = new Headers(request.headers);
    requestHeaders.set('x-nonce', nonce);
    requestHeaders.set('Content-Security-Policy', cspValue);
    requestHeaders.set(
      'Content-Security-Policy-Report-Only',
      cspReportOnlyValue
    );
    return NextResponse.next({ request: { headers: requestHeaders } });
  };

  let supabaseResponse = forwardHeaders();

  const supabase = createServerClient(supabaseUrl, anonKey, {
    cookies: {
      getAll() {
        return request.cookies.getAll();
      },
      setAll(cookiesToSet) {
        for (const { name, value } of cookiesToSet) {
          request.cookies.set(name, value);
        }
        // Re-create after mutating request.cookies so the rebuilt request
        // headers carry the refreshed auth cookies *and* the nonce/CSP.
        supabaseResponse = forwardHeaders();
        for (const { name, value, options } of cookiesToSet) {
          supabaseResponse.cookies.set(name, value, options);
        }
      },
    },
  });

  // IMPORTANT: Do not add code between createServerClient and auth.getUser().
  // A simple mistake could make it very hard to debug issues with users being
  // randomly logged out.
  const {
    data: { user },
  } = await supabase.auth.getUser();

  const { pathname, search } = request.nextUrl;

  // API routes handle their own 401s (returning JSON, not redirecting), so
  // we deliberately don't run the redirect-to-/login dance here. Letting
  // the middleware run is what makes ``getUser()`` above fire — its side
  // effect is refreshing the access token via the cookie adapter when the
  // current one is expiring, which keeps every authenticated /api/* call
  // from 401-ing the moment the session crosses the JWT TTL.
  if (pathname.startsWith('/api/')) {
    supabaseResponse.headers.set('Content-Security-Policy', cspValue);
    supabaseResponse.headers.set(
      'Content-Security-Policy-Report-Only',
      cspReportOnlyValue
    );
    return supabaseResponse;
  }

  // Public marketing landing page. Signed-in users get sent to the dashboard
  // so they don't see the marketing pitch; everyone else can view it.
  if (pathname === '/') {
    if (user) {
      const url = request.nextUrl.clone();
      url.pathname = '/dashboard';
      url.search = '';
      return NextResponse.redirect(url);
    }
    supabaseResponse.headers.set('Content-Security-Policy', cspValue);
    supabaseResponse.headers.set(
      'Content-Security-Policy-Report-Only',
      cspReportOnlyValue
    );
    return supabaseResponse;
  }

  if (pathname.startsWith('/login') || pathname.startsWith('/auth')) {
    if (user && pathname.startsWith('/login')) {
      const url = request.nextUrl.clone();
      url.pathname = safeNext(request.nextUrl.searchParams.get('next'));
      url.search = '';
      return NextResponse.redirect(url);
    }
    supabaseResponse.headers.set('Content-Security-Policy', cspValue);
    supabaseResponse.headers.set(
      'Content-Security-Policy-Report-Only',
      cspReportOnlyValue
    );
    return supabaseResponse;
  }

  // Public legal pages — readable by everyone, signed in or not (unlike `/`,
  // which bounces signed-in users to /dashboard). They must be reachable
  // pre-signup: prospective users read them before creating an account, and
  // the payment processor requires public Terms/Privacy URLs.
  if (pathname === '/terms' || pathname === '/privacy') {
    supabaseResponse.headers.set('Content-Security-Policy', cspValue);
    supabaseResponse.headers.set(
      'Content-Security-Policy-Report-Only',
      cspReportOnlyValue
    );
    return supabaseResponse;
  }

  // Public job search (#467 §10) — the auth-adaptive `/search` surface serves
  // logged-out visitors (the growth funnel) as well as signed-in users at the
  // same URL. Like the legal pages above, it must be reachable WITHOUT a
  // session, so it's allowlisted here before the redirect-to-/login gate below.
  // TARGETED: the exact `/search` path, plus UUID-shaped `/search/<id>` detail
  // paths only (SEARCH_DETAIL_RE — the shareable listing URLs, §11.2). Every
  // other `(app)/*` route stays fully gated (an anonymous hit to `/jobs`,
  // `/settings`, … still redirects to /login). The page/layout branch shell +
  // rendering on `getUser()` themselves.
  if (pathname === '/search' || SEARCH_DETAIL_RE.test(pathname)) {
    supabaseResponse.headers.set('Content-Security-Policy', cspValue);
    supabaseResponse.headers.set(
      'Content-Security-Policy-Report-Only',
      cspReportOnlyValue
    );
    return supabaseResponse;
  }

  if (!user) {
    const url = request.nextUrl.clone();
    url.pathname = '/login';
    url.search = '';
    url.searchParams.set('next', pathname + search);
    return NextResponse.redirect(url);
  }

  supabaseResponse.headers.set('Content-Security-Policy', cspValue);
  supabaseResponse.headers.set(
    'Content-Security-Policy-Report-Only',
    cspReportOnlyValue
  );
  return supabaseResponse;
}

export const config = {
  matcher: [
    {
      // Bypass: Next internals, favicons, manifest/robots/sitemap, brand
      // assets at the public root, and the /public/images directory
      // (public-page assets like the hero screenshot are served from here and
      // must not require auth).
      //
      // The root-level brand assets are listed EXPLICITLY (#899). They live at
      // /logo.png rather than under /images/, so the original pattern bounced
      // them to /login — and an email client is never authenticated, so the
      // logo was broken in every auth email we sent. Same for the touch/PWA
      // icons: site.webmanifest is publicly readable and points at
      // android-chrome-*.png, which was not.
      //
      // Listed by name rather than by "anything with a file extension" on
      // purpose — a blanket extension bypass would take real routes out of the
      // auth check the first time one ends in a dot.
      //
      // ``/api/*`` is intentionally NOT bypassed — the middleware's
      // ``auth.getUser()`` call is what refreshes the access token via the
      // cookie adapter, and skipping it on /api/* causes every authenticated
      // route handler to ship a stale token to wyrdfold-api after the JWT
      // TTL elapses. The handler for /api/* exits early in ``proxy()`` so
      // route handlers keep their own 401 contract.
      source:
        '/((?!_next/static|_next/image|favicon|logo\\.|apple-touch-icon|android-chrome-|mstile-|wyrdfold-|images/|site\\.webmanifest|robots\\.txt|sitemap\\.xml).*)',
    },
  ],
};
