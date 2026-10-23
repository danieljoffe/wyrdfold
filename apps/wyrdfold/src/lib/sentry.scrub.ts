import type { Breadcrumb, Event } from '@sentry/nextjs';

// What may leave the app in a Sentry event. `sendDefaultPii: false` is not
// enough on its own: with it, a captured server error still carried the
// Supabase session cookie, the Authorization header, the request body and the
// /auth/callback `?code=` (checked against a stand-in ingest before this
// existed). So every event, transaction and breadcrumb passes through here.

// Request headers worth keeping for debugging. Every other header is dropped:
// cookie, authorization, x-api-key, forwarded IPs, the BFF secret, …
const SAFE_REQUEST_HEADERS = new Set([
  'accept',
  'accept-language',
  'content-length',
  'content-type',
  'host',
  'referer',
  'user-agent',
]);

// Span and trace attributes that hold raw headers (cookies included), bodies
// or query strings.
const UNSAFE_ATTRIBUTE =
  /^(http\.(request|response)\.header\.|http\.request\.body|url\.query$|http\.query$)/;

// A query string or fragment after an absolute URL, or after a path that
// starts a string or follows a space, quote, bracket, `=` or `,`. Query
// strings carry auth codes (`?code=`, `?token_hash=`), search terms and
// filters.
const URL_QUERY = /(https?:\/\/[^\s?#"'<>]*)[?#][^\s"'<>]*/g;
const PATH_QUERY = /(^|[\s"'(=,])(\/[^\s?#"'<>]*)[?#][^\s"'<>]*/g;

// Passed through untouched. Stack frames feed grouping and symbolication and
// hold no request data. `sdkProcessingMetadata` is the SDK's own scratch
// space: it is deleted before an event is sent, it holds scope objects with
// circular references, and the envelope's trace header is read from it.
const VERBATIM_KEYS = new Set([
  'stacktrace',
  'debug_meta',
  'sdkProcessingMetadata',
]);

/** Drops the query string and fragment from every URL or path in `value`. */
export function stripQueries(value: string): string {
  return value.replace(URL_QUERY, '$1').replace(PATH_QUERY, '$1$2');
}

// A copy with every string query-stripped. `seen` maps each object already
// copied to its copy, so shared and circular references survive.
function stripQueriesDeep<T>(
  node: T,
  seen = new WeakMap<object, unknown>()
): T {
  if (typeof node === 'string') return stripQueries(node) as T;
  if (!node || typeof node !== 'object') return node;
  const known = seen.get(node);
  if (known !== undefined) return known as T;
  if (Array.isArray(node)) {
    const copy: unknown[] = [];
    seen.set(node, copy);
    for (const item of node) copy.push(stripQueriesDeep(item, seen));
    return copy as T;
  }
  const copy: Record<string, unknown> = {};
  seen.set(node, copy);
  for (const [key, value] of Object.entries(node)) {
    copy[key] = VERBATIM_KEYS.has(key) ? value : stripQueriesDeep(value, seen);
  }
  return copy as T;
}

function safeAttributes<T extends Record<string, unknown>>(data: T): T {
  return Object.fromEntries(
    Object.entries(data).filter(([key]) => !UNSAFE_ATTRIBUTE.test(key))
  ) as T;
}

/**
 * Returns a copy of `event` that is safe to send: no cookies, bodies, query
 * strings or non-allowlisted headers, no console breadcrumbs, and a user
 * reduced to its id. Used for errors and transactions on every runtime.
 */
export function scrubEvent<T extends Event>(event: T): T {
  const scrubbed = stripQueriesDeep(event);
  if (scrubbed.request) {
    const { method, url, headers } = scrubbed.request;
    scrubbed.request = {
      ...(method === undefined ? {} : { method }),
      ...(url === undefined ? {} : { url }),
      ...(headers === undefined
        ? {}
        : {
            headers: Object.fromEntries(
              Object.entries(headers).filter(([name]) =>
                SAFE_REQUEST_HEADERS.has(name.toLowerCase())
              )
            ),
          }),
    };
  }
  const trace = scrubbed.contexts?.trace;
  if (trace?.data) trace.data = safeAttributes(trace.data);
  if (scrubbed.spans) {
    scrubbed.spans = scrubbed.spans.map(span =>
      span.data ? { ...span, data: safeAttributes(span.data) } : span
    );
  }
  if (scrubbed.breadcrumbs) {
    scrubbed.breadcrumbs = scrubbed.breadcrumbs.filter(
      breadcrumb => breadcrumb.category !== 'console'
    );
  }
  if (scrubbed.user?.id === undefined) delete scrubbed.user;
  else scrubbed.user = { id: scrubbed.user.id };
  return scrubbed;
}

/**
 * Drops console breadcrumbs and strips query strings from the rest. Console
 * output can hold anything (résumé text, prompts, tokens) and has no scrubber,
 * which is also why Sentry Logs stays off (#29 H-r2-5).
 */
export function scrubBreadcrumb(breadcrumb: Breadcrumb): Breadcrumb | null {
  if (breadcrumb.category === 'console') return null;
  return stripQueriesDeep(breadcrumb);
}
