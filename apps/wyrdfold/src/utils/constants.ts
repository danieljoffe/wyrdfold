// CSP allow-list for WyrdFold. Trimmed from root: no hCaptcha/Calendly/
// Storybook iframes since the admin app doesn't embed them.

const SUPABASE_STORAGE_URL = 'https://*.supabase.co';
const SENTRY_URL = 'https://*.sentry.io';
const SENTRY_INGEST_URL = 'https://*.ingest.sentry.io';

export const allowedImageOrigins = [SUPABASE_STORAGE_URL];

// #470: company-logo link hosts — the client BUILDS these URLs from the
// stored company domain (no images are ever stored). IMG-SRC ONLY: these
// are deliberately kept out of `allowedOrigins`/connect-src — the app never
// fetches from them, browsers only render <img> tags.
//
// These hosts receive the visitor's IP and the company domain being viewed,
// which is a third-party data flow the privacy policy must name — the
// `legalPages` spec fails if a host here is missing from it. Google's
// favicon endpoint was DROPPED for that reason (#470 review): it is an
// advertising company's host that can correlate the request to an
// identified user via its own SameSite=None cookies, which does not sit
// well beside the policy's "no advertising or cross-site tracking cookies"
// claim. Brandfetch is a contractual CDN whose designed use is hotlinking;
// DuckDuckGo is privacy-positioned. Two tiers plus the initials monogram
// is enough of a cascade.
export const logoImageOrigins = [
  'https://cdn.brandfetch.io',
  'https://icons.duckduckgo.com',
];

export const allowedOrigins = [
  ...allowedImageOrigins,
  SENTRY_URL,
  SENTRY_INGEST_URL,
];
