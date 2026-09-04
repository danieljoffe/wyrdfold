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
export const logoImageOrigins = [
  'https://cdn.brandfetch.io',
  'https://icons.duckduckgo.com',
  'https://www.google.com',
];

export const allowedOrigins = [
  ...allowedImageOrigins,
  SENTRY_URL,
  SENTRY_INGEST_URL,
];
