import { NextRequest, NextResponse } from 'next/server';
import { createServerClient } from '@supabase/ssr';
import { allowedOrigins, allowedImageOrigins } from '@/utils/constants';
import { isProduction } from '@/utils/helpers';

// Default post-auth destination for signed-in users. The marketing landing
// page lives at `/`; authenticated users belong on the dashboard.
const HOME_DEFAULT = '/dashboard';

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
  return supabaseResponse;
}

export const config = {
  matcher: [
    {
      // Bypass: Next internals, favicons, manifest/robots/sitemap, and the
      // /public/images directory (public-page assets like the hero screenshot
      // are served from here and must not require auth).
      //
      // ``/api/*`` is intentionally NOT bypassed — the middleware's
      // ``auth.getUser()`` call is what refreshes the access token via the
      // cookie adapter, and skipping it on /api/* causes every authenticated
      // route handler to ship a stale token to wyrdfold-api after the JWT
      // TTL elapses. The handler for /api/* exits early in ``proxy()`` so
      // route handlers keep their own 401 contract.
      source:
        '/((?!_next/static|_next/image|favicon|images/|site\\.webmanifest|robots\\.txt|sitemap\\.xml).*)',
    },
  ],
};
