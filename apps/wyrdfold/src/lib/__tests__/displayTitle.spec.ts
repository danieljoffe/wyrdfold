import { displayTitle } from '../displayTitle';

describe('displayTitle', () => {
  it('prefers the server-cleaned form when present', () => {
    expect(
      displayTitle({
        title: 'senior ai engineer iii',
        title_display: 'Senior AI Engineer III',
      })
    ).toBe('Senior AI Engineer III');
  });

  it('falls back to the raw title when the column is null', () => {
    expect(
      displayTitle({ title: 'Senior Engineer', title_display: null })
    ).toBe('Senior Engineer');
  });

  it('falls back when the column is absent (RPC list paths, stage 2)', () => {
    expect(displayTitle({ title: 'Senior Engineer' })).toBe('Senior Engineer');
  });
});
