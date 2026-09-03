import { pickErrorMessage } from '../pickErrorMessage';

// The BFF's shared error-message ladder (#971 §2) — previously three
// copy-pasted implementations (waitlist + the public listings/search twins).
describe('pickErrorMessage', () => {
  const FALLBACK = 'Something went wrong.';

  it('prefers a string detail over a string error', () => {
    expect(
      pickErrorMessage(
        { detail: 'From the API.', error: 'From the BFF.' },
        FALLBACK
      )
    ).toBe('From the API.');
  });

  it('falls back to a string error when detail is absent or structured', () => {
    expect(pickErrorMessage({ error: 'From the BFF.' }, FALLBACK)).toBe(
      'From the BFF.'
    );
    expect(
      pickErrorMessage(
        { detail: { code: 'x' }, error: 'From the BFF.' },
        FALLBACK
      )
    ).toBe('From the BFF.');
  });

  it('ignores blank strings — the old twins let "" overwrite the fallback', () => {
    expect(pickErrorMessage({ detail: '   ' }, FALLBACK)).toBe(FALLBACK);
    expect(pickErrorMessage({ detail: '', error: '  ' }, FALLBACK)).toBe(
      FALLBACK
    );
  });

  it('returns the fallback for non-objects and null', () => {
    expect(pickErrorMessage(null, FALLBACK)).toBe(FALLBACK);
    expect(pickErrorMessage('raw text', FALLBACK)).toBe(FALLBACK);
    expect(pickErrorMessage(undefined, FALLBACK)).toBe(FALLBACK);
  });
});
