import { stripHtmlToText } from '../stripHtml';

/** Reference-JD snippets rendered stored HTML verbatim as text (#606). */
describe('stripHtmlToText', () => {
  it('strips the observed prod reference-JD markup to readable text', () => {
    expect(
      stripHtmlToText(
        '<p style="min-height:1.5em"></p><h1><strong>Hadrian - Manufacturing the Future</strong></h1><p style="min-height:1.5em"></p><p>Hadrian is building autonomous factories.</p>'
      )
    ).toBe(
      'Hadrian - Manufacturing the Future Hadrian is building autonomous factories.'
    );
  });

  it('decodes common entities and drops script/style wholesale', () => {
    expect(
      stripHtmlToText('Tools &amp; Dies<style>.x{}</style> &nbsp;ok')
    ).toBe('Tools & Dies ok');
  });

  it('passes plain text through', () => {
    expect(stripHtmlToText('Just a plain description.')).toBe(
      'Just a plain description.'
    );
  });
});
