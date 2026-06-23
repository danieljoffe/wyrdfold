// ``isomorphic-dompurify`` re-exports the ``DOMPurify`` instance type from
// ``dompurify``; we import from the former because that is the package the
// app actually depends on (``dompurify`` is only a transitive dep and is
// not resolvable as a direct type import).
import type { DOMPurify } from 'isomorphic-dompurify';

/**
 * Hardened client-side sanitizer for attacker-controlled job-description
 * HTML (audit #29, rounds 2-3).
 *
 * The upstream JD body is third-party (Greenhouse et al.) and merely
 * *passes through* our poller — it is NOT authored by us and must be
 * treated as attacker-controlled at render time. The server already runs
 * ``bleach`` over it (``apps/wyrdfold-api/app/services/sanitize.py``), but
 * this is the last line of defence before ``dangerouslySetInnerHTML``, so
 * we mirror the server's allow-list here rather than trusting the broad
 * DOMPurify ``html`` profile.
 *
 * DOMPurify's ``USE_PROFILES: { html: true }`` blocks the core XSS vectors
 * (``<script>``, ``javascript:`` URIs) but still permits CSS injection via
 * ``style``, outbound ``<img>`` beacons, DOM clobbering, in-app ``<form>``
 * phishing, ``<iframe>``/``<embed>``/``<object>``/SVG, and ``data:`` URIs.
 * The explicit allow-list below forbids all of those.
 *
 * The allow-list is an exact mirror of the server's ``ALLOWED_TAGS`` /
 * ``ALLOWED_ATTRS`` / ``ALLOWED_PROTOCOLS`` so legitimate JD formatting
 * (headings, lists, emphasis, links, code blocks) survives unchanged.
 */

// Mirrors ALLOWED_TAGS in app/services/sanitize.py. Note ``img`` is
// deliberately omitted (server omits it too) to kill tracking-beacon
// ``<img src=x onerror=...>`` payloads.
export const ALLOWED_TAGS = [
  'p',
  'br',
  'ul',
  'ol',
  'li',
  'strong',
  'em',
  'b',
  'i',
  'u',
  'a',
  'h1',
  'h2',
  'h3',
  'h4',
  'h5',
  'h6',
  'blockquote',
  'code',
  'pre',
  'span',
  'div',
] as const;

// Based on ALLOWED_ATTRS in app/services/sanitize.py. DOMPurify's
// ``ALLOWED_ATTR`` is a flat list (not per-tag), so this is the union of
// the server's per-tag attribute allow-list: ``a`` may carry
// ``href``/``title``/``rel``; ``span``/``div`` carry none.
//
// ``target`` is the ONE deliberate divergence from the server: we permit
// it so an upstream ``target="_blank"`` link still opens in a new tab, but
// the ``afterSanitizeAttributes`` hook below force-rewrites ``rel`` to
// ``noopener noreferrer`` on every targeted anchor (audit #29 R2-3:
// "target=_blank without rel=noopener"). The hook must SEE ``target`` to
// fire, which is why ``target`` is allowed here rather than forbidden.
export const ALLOWED_ATTR = ['href', 'title', 'rel', 'target'] as const;

// Mirrors ALLOWED_PROTOCOLS (http, https, mailto). Anchors/hrefs that are
// not http(s)/mailto — e.g. ``javascript:``, ``data:``, ``vbscript:`` — are
// stripped. Relative URLs (``#anchor``, ``/path``, ``./x``, ``?q=1``) are
// still permitted, matching DOMPurify's default relative-URL behaviour.
export const ALLOWED_URI_REGEXP =
  /^(?:(?:https?|mailto):|[^a-z]|[a-z+.-]+(?:[^a-z+.:-]|$))/i;

// Belt-and-braces: even though these are absent from ALLOWED_TAGS/ATTR,
// name them explicitly so a future edit to the allow-list cannot silently
// re-admit the dangerous surfaces (CSS injection, framing, SVG/MathML
// mutation-XSS vectors, in-app form phishing, tracking beacons).
const FORBID_TAGS = [
  'style',
  'form',
  'input',
  'button',
  'textarea',
  'select',
  'option',
  'iframe',
  'embed',
  'object',
  'svg',
  'math',
  'img',
  'video',
  'audio',
  'source',
  'link',
  'base',
  'meta',
];

// ``target`` is intentionally NOT forbidden here — it is allow-listed
// above so the rel hook can rewrite it (see ALLOWED_ATTR comment).
const FORBID_ATTR = ['style', 'srcset', 'src', 'formaction'];

let hookedInstance: DOMPurify | null = null;

/**
 * Register a one-time ``afterSanitizeAttributes`` hook on the given
 * DOMPurify instance that forces ``rel="noopener noreferrer"`` on any
 * anchor that opens a new browsing context. Without ``noopener`` the
 * opened page can reach back via ``window.opener`` (reverse tabnabbing).
 *
 * Idempotent per instance — adding the hook twice would run it twice.
 */
function ensureRelHook(purifier: DOMPurify): void {
  if (hookedInstance === purifier) return;
  purifier.addHook('afterSanitizeAttributes', node => {
    // ``node`` is an Element here (afterSanitizeAttributes hook).
    if (node.tagName === 'A' && node.hasAttribute('target')) {
      node.setAttribute('rel', 'noopener noreferrer');
    }
  });
  hookedInstance = purifier;
}

/**
 * Decode one level of HTML entities, then sanitize, in a single trusted
 * step. The upstream body arrives entity-encoded (``&lt;h4&gt;`` rather
 * than ``<h4>``), so it must be decoded once before DOMPurify can see the
 * tags. We decode the *raw* value and sanitize the result directly — the
 * decoded string is never assigned anywhere live, so there is no window in
 * which un-sanitized markup is reachable.
 *
 * @param raw       The entity-encoded upstream JD HTML.
 * @param purifier  A DOMPurify instance (passed in so the helper stays
 *                  pure / SSR-safe and trivially unit-testable).
 */
export function sanitizeJobDescriptionHtml(
  raw: string,
  purifier: DOMPurify
): string {
  if (!raw || !raw.trim()) return '';

  ensureRelHook(purifier);

  // Decode one level of HTML entities. ``textarea.innerHTML`` ->
  // ``.value`` is the canonical browser entity-decode and does NOT
  // execute or materialise any markup (textarea content is raw text).
  const ta = document.createElement('textarea');
  ta.innerHTML = raw;
  const decoded = ta.value;

  return purifier.sanitize(decoded, {
    ALLOWED_TAGS: [...ALLOWED_TAGS],
    ALLOWED_ATTR: [...ALLOWED_ATTR],
    ALLOWED_URI_REGEXP,
    FORBID_TAGS,
    FORBID_ATTR,
    // Keep the TEXT of stripped wrapper tags (mirrors the server's
    // bleach ``strip=True``, which removes disallowed tags but preserves
    // their inner text). DOMPurify still drops the *content* of the truly
    // dangerous tags — ``<script>``/``<style>``/``<svg>`` are in its
    // built-in ``FORBID_CONTENTS`` set, so their bodies never leak through.
    // (Setting this to ``false`` instead would strip ALL text, including
    // legitimate ``<p>Hello</p>`` body copy — verified empirically.)
    KEEP_CONTENT: true,
    // Disallow ``data-*`` pass-through; JD bodies never legitimately carry
    // them and they widen the attribute surface (DOM-clobbering vectors).
    ALLOW_DATA_ATTR: false,
    // Neutralise DOM-clobbering: prefix id/name collisions so injected
    // ``<a id="x">`` cannot shadow ``document.x`` / form-property lookups.
    SANITIZE_DOM: true,
    SANITIZE_NAMED_PROPS: true,
  });
}
