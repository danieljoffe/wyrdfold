/**
 * Company display names (#606). Boards deliver slugs and title-cased
 * mangles which we previously rendered verbatim: "Geaerospace",
 * "Redhat", "Oclc", "hinge-health", "rox-data-corp". Display-side
 * repair only — stored identifiers are untouched.
 *
 * Rules, in order:
 *   1. exact override for observed mangles (case-insensitive);
 *   2. hyphenated slugs de-slug to spaced Title Case;
 *   3. single all-lowercase tokens get a capital;
 *   4. anything else (mixed case, spaces, dots, ®…) passes through.
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
  const trimmed = raw.trim();
  if (trimmed.length === 0) return raw;

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
