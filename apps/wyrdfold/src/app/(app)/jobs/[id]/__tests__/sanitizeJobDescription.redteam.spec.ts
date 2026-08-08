/**
 * Red-team round on the JD sanitizer (2026-08-08 overnight sweep).
 *
 * The existing spec covers the vectors the audit named. This file attacks the
 * two things that spec does NOT model, both specific to how this sanitizer
 * differs from a stock DOMPurify call:
 *
 *   1. THE ENTITY PRE-DECODE. `sanitizeJobDescriptionHtml` decodes one level
 *      of HTML entities *before* handing the string to DOMPurify, because the
 *      upstream body arrives entity-encoded. That decode is the one place
 *      markup can be *created* rather than removed, so it is where a bypass
 *      would live: anything the server escaped is re-materialised here and
 *      DOMPurify is the only thing left standing.
 *
 *   2. THE ALLOW-LIST'S OWN EDGES. `target` is deliberately allowed so the
 *      rel-rewrite hook can fire; `KEEP_CONTENT: true` deliberately preserves
 *      the text of stripped tags. Both are correct, and both are the kind of
 *      deliberate loosening that a later edit turns into a hole.
 *
 * Every payload here must come out inert.
 */
import createDOMPurify from 'dompurify';
import { sanitizeJobDescriptionHtml } from '../../sanitizeJobDescription';

const DOMPurify = createDOMPurify(window);
const clean = (raw: string): string =>
  sanitizeJobDescriptionHtml(raw, DOMPurify);

/** Render the sanitizer's output and assert nothing executable survived. */
const assertInert = (out: string) => {
  const host = document.createElement('div');
  host.innerHTML = out;
  expect(host.querySelector('script')).toBeNull();
  expect(host.querySelector('iframe')).toBeNull();
  expect(host.querySelector('svg')).toBeNull();
  expect(host.querySelector('img')).toBeNull();
  expect(host.querySelector('style')).toBeNull();
  expect(host.querySelector('form')).toBeNull();
  // No element may carry an inline event handler.
  for (const el of Array.from(host.querySelectorAll('*'))) {
    for (const attr of Array.from(el.attributes)) {
      expect(attr.name.toLowerCase()).not.toMatch(/^on/);
    }
  }
};

describe('entity pre-decode cannot be used to smuggle markup', () => {
  it('single-encoded script tag decodes to text, not an element', () => {
    const out = clean('&lt;script&gt;alert(1)&lt;/script&gt;');
    assertInert(out);
    expect(out).not.toMatch(/<script/i);
  });

  it('double-encoded script tag survives as inert text', () => {
    // Decodes ONE level to `&lt;script&gt;`, which DOMPurify re-escapes.
    const out = clean('&amp;lt;script&amp;gt;alert(1)&amp;lt;/script&amp;gt;');
    assertInert(out);
    expect(out).not.toMatch(/<script/i);
  });

  it('encoded event handler on an allowed tag is dropped', () => {
    const out = clean(
      '&lt;p onmouseover=&quot;alert(1)&quot;&gt;hover&lt;/p&gt;'
    );
    assertInert(out);
    expect(out.toLowerCase()).not.toContain('onmouseover');
  });

  it('encoded javascript: href is dropped', () => {
    const out = clean(
      '&lt;a href=&quot;javascript:alert(1)&quot;&gt;go&lt;/a&gt;'
    );
    assertInert(out);
    expect(out.toLowerCase()).not.toContain('javascript:');
  });

  it('numeric-entity encoded javascript: href is dropped', () => {
    const out = clean(
      '<a href="&#106;&#97;&#118;&#97;&#115;&#99;&#114;&#105;&#112;&#116;&#58;alert(1)">go</a>'
    );
    assertInert(out);
    expect(out.toLowerCase()).not.toContain('javascript:');
  });

  it('mixed-case and whitespace-obfuscated javascript: href is dropped', () => {
    const out = clean('<a href="JaVaScRiPt&#09;:alert(1)">go</a>');
    assertInert(out);
    expect(out.toLowerCase()).not.toMatch(/javascript\s*:/);
  });

  it('data: URI href is dropped', () => {
    const out = clean(
      '<a href="data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==">x</a>'
    );
    assertInert(out);
    expect(out.toLowerCase()).not.toContain('data:');
  });
});

describe('allow-list edges stay closed', () => {
  it('KEEP_CONTENT preserves text but never the stripped tag itself', () => {
    const out = clean('<div><style>body{display:none}</style>Real copy</div>');
    assertInert(out);
    expect(out.toLowerCase()).not.toContain('display:none');
    expect(out).toContain('Real copy');
  });

  it('an anchor with target always gets rel=noopener noreferrer', () => {
    const out = clean('<a href="https://evil.example" target="_blank">go</a>');
    const host = document.createElement('div');
    host.innerHTML = out;
    const a = host.querySelector('a');
    expect(a?.getAttribute('rel')).toBe('noopener noreferrer');
  });

  it('an attacker-supplied rel cannot override the hook', () => {
    const out = clean(
      '<a href="https://evil.example" target="_blank" rel="opener">go</a>'
    );
    const host = document.createElement('div');
    host.innerHTML = out;
    expect(host.querySelector('a')?.getAttribute('rel')).toBe(
      'noopener noreferrer'
    );
  });

  it('style attribute is stripped from allowed tags (CSS injection)', () => {
    const out = clean(
      '<p style="position:fixed;inset:0;background:url(https://evil.example/x)">hi</p>'
    );
    assertInert(out);
    expect(out.toLowerCase()).not.toContain('style');
    expect(out).toContain('hi');
  });

  it('DOM clobbering via id/name on allowed tags is neutralised', () => {
    const out = clean(
      '<a id="body" name="cookie" href="https://x.example">c</a>'
    );
    const host = document.createElement('div');
    host.innerHTML = out;
    const a = host.querySelector('a');
    // SANITIZE_DOM / SANITIZE_NAMED_PROPS: the raw clobbering names must not
    // survive verbatim on the element.
    expect(a?.getAttribute('id')).not.toBe('body');
    expect(a?.getAttribute('name')).not.toBe('cookie');
  });

  it('nested/malformed markup does not reassemble into a script', () => {
    const out = clean('<scr<script>ipt>alert(1)</scr</script>ipt>');
    assertInert(out);
    expect(out).not.toMatch(/<script/i);
  });

  it('mXSS via nested noscript/template does not resurrect a payload', () => {
    const out = clean(
      '<noscript><p title="</noscript><img src=x onerror=alert(1)>">'
    );
    assertInert(out);
  });

  it('an empty or whitespace body short-circuits to empty string', () => {
    expect(clean('')).toBe('');
    expect(clean('   \n\t ')).toBe('');
  });
});
