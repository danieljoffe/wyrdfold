/**
 * Plain-text excerpt from stored HTML (#606). Reference-JD snippets are
 * saved as the board's raw HTML; the target detail page rendered that
 * markup verbatim as text ("<p style=\"min-height:1.5em\"></p><h1>…").
 * Same defect class as the #505/#527 listing tag soup.
 *
 * Deliberately regex-based (SSR-safe, no DOM dependency) — this produces
 * a display excerpt, never sanitized HTML for re-injection.
 */
const ENTITIES: Record<string, string> = {
  '&amp;': '&',
  '&lt;': '<',
  '&gt;': '>',
  '&quot;': '"',
  '&#39;': "'",
  '&nbsp;': ' ',
};

export function stripHtmlToText(html: string): string {
  const withoutTags = html
    .replace(/<(script|style)[^>]*>[\s\S]*?<\/\1>/gi, ' ')
    .replace(/<[^>]+>/g, ' ');
  const decoded = withoutTags.replace(
    /&(amp|lt|gt|quot|#39|nbsp);/g,
    m => ENTITIES[m] ?? m
  );
  return decoded.replace(/\s+/g, ' ').trim();
}
