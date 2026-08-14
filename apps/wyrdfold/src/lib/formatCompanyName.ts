/**
 * Company display names (#606). Boards deliver slugs and title-cased
 * mangles which we previously rendered verbatim: "Geaerospace",
 * "Redhat", "Oclc", "hinge-health", "rox-data-corp". Display-side
 * repair only — stored identifiers are untouched.
 *
 * Rules, in order:
 *   1. leading feed-index junk is stripped — a ZERO-PADDED digit prefix
 *      ("003 Humana Inc.") is board catalog junk, never a name; genuinely
 *      numeric names ("24 Hour Fitness", "3M", "7-Eleven", "37signals")
 *      have no leading zero or no following space and pass through
 *      (onboarding-sweep-2026-08-14 A5);
 *   2. exact override for observed mangles (case-insensitive);
 *   3. hyphenated slugs de-slug to spaced Title Case;
 *   4. single all-lowercase tokens get a capital;
 *   5. anything else (mixed case, spaces, dots, ®…) passes through.
 */
const OVERRIDES: Record<string, string> = {
  redhat: 'Red Hat',
  geaerospace: 'GE Aerospace',
  oclc: 'OCLC',
  wgu: 'WGU',
  ngc: 'NGC',
  jj: 'JJ',
  icapital: 'iCapital',
  spacex: 'SpaceX',
};

function titleCaseToken(token: string): string {
  if (token.length === 0) return token;
  return token[0]?.toUpperCase() + token.slice(1);
}

export function formatCompanyName(raw: string): string {
  let trimmed = raw.trim();
  if (trimmed.length === 0) return raw;

  // Zero-padded numeric prefix = feed catalog index, not part of the name
  // ("003 Humana Inc." → "Humana Inc."). The leading zero is the
  // discriminator: real numeric names ("24 Hour Fitness") never carry one.
  // Keep the original if stripping would leave nothing.
  const dejunked = trimmed.replace(/^0\d*\s+/, '');
  if (dejunked.length > 0) trimmed = dejunked;

  const override = OVERRIDES[trimmed.toLowerCase()];
  if (override) return override;

  // Slug shapes: all-lowercase-ish with hyphens ("hinge-health",
  // "rox-data-corp"). Names that already contain spaces or uppercase
  // beyond the first letter are left alone.
  if (trimmed.includes('-') && trimmed === trimmed.toLowerCase()) {
    return trimmed.split('-').filter(Boolean).map(titleCaseToken).join(' ');
  }

  if (/^[a-z0-9.]+$/.test(trimmed)) {
    return titleCaseToken(trimmed);
  }

  return trimmed;
}
