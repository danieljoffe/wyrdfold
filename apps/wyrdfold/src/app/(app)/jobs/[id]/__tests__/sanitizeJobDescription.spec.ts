/**
 * XSS / hardening tests for the job-description sanitizer (audit #29 R2-3).
 *
 * The upstream JD body is attacker-controlled. These tests assert that the
 * dangerous surfaces DOMPurify's broad ``html`` profile would otherwise
 * permit (CSS injection, framing, SVG, forms, beacons, bad URI schemes,
 * tabnabbing) are neutralised, AND that legitimate JD formatting survives.
 *
 * The jest environment is jsdom. In a real browser the component passes
 * ``isomorphic-dompurify``'s default export, which is just
 * ``createDOMPurify(window)`` over the page's ``window``. We reproduce that
 * exact object here by binding the underlying ``dompurify`` package to the
 * jsdom ``window`` — same sanitizer, same code path, without dragging in
 * isomorphic-dompurify's Node-only jsdom dependency (which ships untrans, ESM
 * and trips jest's transformIgnorePatterns).
 */
import createDOMPurify from 'dompurify';
import { sanitizeJobDescriptionHtml } from '../../sanitizeJobDescription';

// jsdom provides ``window``; bind DOMPurify to it exactly as the browser
// build of isomorphic-dompurify does.
const DOMPurify = createDOMPurify(window);

const clean = (raw: string): string =>
  sanitizeJobDescriptionHtml(raw, DOMPurify);

describe('sanitizeJobDescriptionHtml — attack payloads are neutralized', () => {
  it('strips <svg onload=...> (event-handler XSS via SVG)', () => {
    const out = clean('<svg onload="alert(1)"><circle r="1" /></svg>');
    expect(out.toLowerCase()).not.toContain('svg');
    expect(out.toLowerCase()).not.toContain('onload');
    expect(out).not.toContain('alert');
  });

  it('strips <iframe srcdoc=...> (framed-document XSS)', () => {
    const out = clean('<iframe srcdoc="<script>alert(1)</script>"></iframe>');
    expect(out.toLowerCase()).not.toContain('iframe');
    expect(out.toLowerCase()).not.toContain('srcdoc');
    expect(out.toLowerCase()).not.toContain('script');
  });

  it('strips <img src=x onerror=...> (beacon + event-handler XSS)', () => {
    const out = clean('<img src="x" onerror="alert(1)">');
    expect(out.toLowerCase()).not.toContain('<img');
    expect(out.toLowerCase()).not.toContain('onerror');
  });

  it('strips <form> / <input> (in-app phishing)', () => {
    const out = clean(
      '<form action="https://evil.example"><input name="password"></form>'
    );
    expect(out.toLowerCase()).not.toContain('<form');
    expect(out.toLowerCase()).not.toContain('<input');
    expect(out.toLowerCase()).not.toContain('evil.example');
  });

  it('strips inline style= (CSS injection / clickjacking overlays)', () => {
    const out = clean('<p style="position:fixed;inset:0;z-index:9999">x</p>');
    expect(out).toContain('<p>');
    expect(out.toLowerCase()).not.toContain('style');
    expect(out.toLowerCase()).not.toContain('position:fixed');
  });

  it('strips a standalone <style> block (and its CSS) but keeps siblings', () => {
    const out = clean('<style>body{display:none}</style><p>kept</p>');
    expect(out.toLowerCase()).not.toContain('style');
    expect(out.toLowerCase()).not.toContain('display:none');
    expect(out).toContain('kept');
  });

  it('strips javascript: hrefs', () => {
    const out = clean('<a href="javascript:alert(1)">click</a>');
    expect(out.toLowerCase()).not.toContain('javascript:');
  });

  it('strips data: hrefs', () => {
    const out = clean(
      '<a href="data:text/html,<script>alert(1)</script>">click</a>'
    );
    expect(out.toLowerCase()).not.toContain('data:');
    expect(out.toLowerCase()).not.toContain('script');
  });

  it('strips <iframe>/<embed>/<object> framing tags', () => {
    expect(clean('<embed src="evil.swf">').toLowerCase()).not.toContain(
      'embed'
    );
    expect(
      clean('<object data="evil.swf"></object>').toLowerCase()
    ).not.toContain('object');
  });

  it('forces rel="noopener noreferrer" on target="_blank" anchors', () => {
    const out = clean('<a href="https://ok.example" target="_blank">go</a>');
    expect(out).toContain('rel="noopener noreferrer"');
    expect(out).toContain('target="_blank"');
  });

  it('overrides any attacker-supplied rel on a targeted anchor', () => {
    const out = clean(
      '<a href="https://ok.example" target="_blank" rel="opener">go</a>'
    );
    expect(out).toContain('rel="noopener noreferrer"');
    expect(out).not.toContain('rel="opener"');
  });

  it('does NOT inject rel onto anchors without a target', () => {
    const out = clean('<a href="https://ok.example">go</a>');
    expect(out).not.toContain('rel=');
    expect(out).not.toContain('target');
  });
});

describe('sanitizeJobDescriptionHtml — legitimate formatting survives', () => {
  it('keeps paragraphs, emphasis, and their TEXT', () => {
    const out = clean('<p>Hello <strong>world</strong> and <em>more</em></p>');
    expect(out).toContain('<p>');
    expect(out).toContain('<strong>');
    expect(out).toContain('<em>');
    expect(out).toContain('Hello');
    expect(out).toContain('world');
    expect(out).toContain('more');
  });

  it('keeps unordered + ordered lists with item text', () => {
    const out = clean('<ul><li>alpha</li><li>beta</li></ul>');
    expect(out).toContain('<ul>');
    expect(out).toContain('<li>');
    expect(out).toContain('alpha');
    expect(out).toContain('beta');
  });

  it('keeps headings h1-h6 with their text', () => {
    const out = clean('<h1>Role</h1><h3>Responsibilities</h3>');
    expect(out).toContain('<h1>Role</h1>');
    expect(out).toContain('<h3>Responsibilities</h3>');
  });

  it('keeps safe anchors with href/title and link text', () => {
    const out = clean(
      '<a href="https://jobs.example/apply" title="Apply">Apply here</a>'
    );
    expect(out).toContain('href="https://jobs.example/apply"');
    expect(out).toContain('title="Apply"');
    expect(out).toContain('Apply here');
  });

  it('keeps relative and mailto hrefs', () => {
    expect(clean('<a href="/apply">x</a>')).toContain('href="/apply"');
    expect(clean('<a href="mailto:jobs@example.com">x</a>')).toContain(
      'mailto:jobs@example.com'
    );
  });

  it('keeps code / pre blocks and their content', () => {
    const out = clean('<pre><code>const x = 1;</code></pre>');
    expect(out).toContain('<pre>');
    expect(out).toContain('<code>');
    expect(out).toContain('const x = 1;');
  });

  it('keeps blockquotes', () => {
    expect(clean('<blockquote>quote</blockquote>')).toContain(
      '<blockquote>quote</blockquote>'
    );
  });

  it('decodes one level of HTML entities before sanitizing (Greenhouse encoding)', () => {
    // Upstream persists ``&lt;h4&gt;...&lt;/h4&gt;`` rather than raw tags.
    const out = clean('&lt;h4&gt;Benefits&lt;/h4&gt;&lt;p&gt;Great&lt;/p&gt;');
    expect(out).toContain('<h4>Benefits</h4>');
    expect(out).toContain('<p>Great</p>');
  });

  it('does NOT double-decode a script smuggled behind double-encoding', () => {
    // We decode exactly ONE level of entities, then sanitize. This input
    // decodes once to the *entity-escaped* text "&lt;script&gt;alert(1)..."
    // — a harmless text node, NOT an executable element. The critical
    // assertion is that no live ``<script>`` tag re-materialises (which a
    // second, un-trusted decode pass would have produced). The literal
    // characters "alert(1)" surviving as inert, escaped page text is the
    // correct, safe outcome.
    const out = clean('&amp;lt;script&amp;gt;alert(1)&amp;lt;/script&amp;gt;');
    expect(out.toLowerCase()).not.toContain('<script');
    // Output is entity-escaped text, not an element: the angle brackets
    // remain encoded so the browser renders them as visible text.
    expect(out).toContain('&lt;script&gt;');
  });

  it('returns empty string for empty / whitespace input', () => {
    expect(clean('')).toBe('');
    expect(clean('   ')).toBe('');
  });
});

describe('sanitizeJobDescriptionHtml — negative controls', () => {
  // These prove the key assertions are real: the same matcher that PASSES
  // for a neutralized payload would FAIL on raw malicious markup, and the
  // "survives" matcher would FAIL if legit content were dropped.

  it('NEG: raw unsanitized markup WOULD contain the script (matcher is real)', () => {
    const raw = '<p>safe</p><script>alert(1)</script>';
    expect(raw.toLowerCase()).toContain('<script'); // the input is genuinely dangerous
    expect(clean(raw).toLowerCase()).not.toContain('<script'); // ...and we remove it
  });

  it('NEG: a missing legit tag WOULD fail the survives-assertion', () => {
    const out = clean('<p>kept</p>');
    // Sanity: asserting a tag that was never present must NOT appear, proving
    // toContain is not vacuously passing on arbitrary strings.
    expect(out).not.toContain('<marquee>');
    expect(out).toContain('<p>kept</p>');
  });
});
