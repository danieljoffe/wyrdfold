/**
 * Operator identity for the legal pages, supplied by the environment.
 *
 * The Terms and Privacy pages must name the operating entity and a physical
 * address. This repository is PUBLIC, so hardcoding them would write a real
 * name and street address into git history permanently — and editing a file
 * later does not redact what is already committed. Env vars keep the repo
 * carrying only the placeholder while production renders the real values.
 *
 * Not `NEXT_PUBLIC_`: both legal pages are server components, so these are
 * read at build time and never enter the client bundle.
 *
 * Missing values fall back to the bracketed placeholder ON PURPOSE. An empty
 * string would render "operated by , " — a grammatical sentence that reads as
 * finished, so an unset variable would ship silently. `[Legal Entity Name]` is
 * unmistakably unfilled to anyone who looks at the page.
 */

const PLACEHOLDER_NAME = '[Legal Entity Name]';
const PLACEHOLDER_ADDRESS = '[registered address]';

function fromEnv(value: string | undefined, placeholder: string): string {
  const trimmed = (value ?? '').trim();
  return trimmed.length > 0 ? trimmed : placeholder;
}

/** The operating entity's legal name, e.g. a sole proprietor or LLC. */
export const LEGAL_ENTITY_NAME = fromEnv(
  process.env.LEGAL_ENTITY_NAME,
  PLACEHOLDER_NAME
);

/** The operating entity's physical address (not a PO Box). */
export const LEGAL_ENTITY_ADDRESS = fromEnv(
  process.env.LEGAL_ENTITY_ADDRESS,
  PLACEHOLDER_ADDRESS
);

/** True when either value is still unfilled — useful for a pre-launch check. */
export const LEGAL_ENTITY_IS_PLACEHOLDER =
  LEGAL_ENTITY_NAME === PLACEHOLDER_NAME ||
  LEGAL_ENTITY_ADDRESS === PLACEHOLDER_ADDRESS;

export const LEGAL_ENTITY_PLACEHOLDERS = {
  name: PLACEHOLDER_NAME,
  address: PLACEHOLDER_ADDRESS,
} as const;
