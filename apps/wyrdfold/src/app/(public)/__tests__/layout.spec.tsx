import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';
import PublicLayout from '../layout';

/**
 * The public header's home link must carry no visible text of its own.
 *
 * In plain terms: the link is named "WyrdFold home" by its aria-label, so any
 * visible words inside it are missing from the name a screen reader or a
 * voice-control user hears — a WCAG 2.5.3 "Label in Name" failure. The "Beta"
 * badge used to sit inside the link, and axe 4.14's
 * `label-content-name-mismatch` rule failed the landing page for it. Same
 * convention as the app sidebar's logo link (see onboarding.spec.ts).
 */
describe('PublicLayout header', () => {
  it('names the home link only by its aria-label, with no visible text inside', () => {
    render(
      <PublicLayout>
        <p>page</p>
      </PublicLayout>
    );

    const home = screen.getByRole('link', { name: 'WyrdFold home' });
    // Exact match: `toHaveTextContent('')` passes for ANY text.
    expect(home.textContent?.trim()).toBe('');
  });

  it('still shows the Beta badge, beside the link rather than inside it', () => {
    render(
      <PublicLayout>
        <p>page</p>
      </PublicLayout>
    );

    const badge = screen.getByText('Beta');
    expect(badge).toBeVisible();
    expect(badge.closest('a')).toBeNull();
  });
});
