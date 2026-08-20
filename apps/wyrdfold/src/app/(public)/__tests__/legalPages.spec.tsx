import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';
import TermsPage from '../terms/page';
import PrivacyPage from '../privacy/page';

/**
 * Guards on the two published legal pages.
 *
 * These are the only pages whose text is a legal instrument, so the properties
 * worth pinning are not visual. Counsel raised each of the ones below during
 * review; a jest failure is a cheaper way to catch a regression than another
 * review round.
 */

// Phrases that belong ONLY to the repository disclaimer (DISCLAIMER.md), never
// to a published policy. Counsel's concern was that a build step might one day
// concatenate the two — the repo disclaimer explains that the rendered pages
// are authoritative, which is nonsense to serve *on* a rendered page.
// `\s+` rather than literal spaces: the source file line-wraps these phrases,
// and rendered textContent collapses whitespace differently again. A guard that
// only matches one particular wrapping is a guard that quietly stops matching.
const DISCLAIMER_ONLY = [
  /repository\s+copies\s+are\s+not\s+the\s+operative\s+policies/i,
  /not\s+the\s+operative\s+agreement/i,
  /the\s+rendered\s+pages\s+govern/i,
  /if\s+you\s+self-host\s+or\s+fork/i,
  /we\s+do\s+not\s+vet,\s+approve,\s+endorse/i,
];

describe.each([
  ['Terms', TermsPage],
  ['Privacy', PrivacyPage],
])('%s page', (name, Page) => {
  it('carries an effective date', () => {
    // Counsel reported this missing twice, reading pasted body text. It is
    // rendered by the page shell above the first section — assert it so the
    // answer stays "yes" rather than depending on who is looking.
    render(<Page />);
    expect(screen.getByText(/effective date:/i)).toBeInTheDocument();
  });

  it('does not contain repository-disclaimer text', () => {
    const { container } = render(<Page />);
    const text = container.textContent ?? '';
    // Guard the guard: a page that rendered nothing would pass every
    // absence check below by vacuity.
    expect(text.length).toBeGreaterThan(2000);
    for (const phrase of DISCLAIMER_ONLY) {
      expect(text).not.toMatch(phrase);
    }
  });

  it('links to its counterpart rather than only naming it', () => {
    // Counsel asked that "Privacy Policy" / "Terms of Service" be real links
    // everywhere they are referenced, not bare text.
    render(<Page />);
    const href = name === 'Terms' ? '/privacy' : '/terms';
    const links = screen
      .getAllByRole('link')
      .filter(a => a.getAttribute('href') === href);
    expect(links.length).toBeGreaterThan(0);
  });
});

describe('claims that must not drift back to an absolute', () => {
  it('does not promise that AI providers never retain anything', () => {
    // The policy describes a CONFIGURATION we control, not a promise binding
    // another company. "never" language here was walked back deliberately:
    // OpenRouter still records request metadata for billing, and provider
    // policies can change independently of our settings.
    const { container } = render(<PrivacyPage />);
    const text = container.textContent ?? '';

    expect(text).toMatch(/Zero-Data-Retention/i);
    expect(text).not.toMatch(/never routed to a provider/i);
    // The limits must be stated, not merely the configuration.
    expect(text).toMatch(/timing, model, token counts/i);
  });

  it('does not claim BYOK is available on the hosted service', () => {
    // Production has no BYOK master key, so no hosted user can supply one.
    const { container } = render(<PrivacyPage />);
    const text = container.textContent ?? '';
    expect(text).toMatch(/not currently offered on the hosted\s+service/i);
  });

  it('says the embedding provider receives experience text', () => {
    // It does — `chunks.upsert_for_optimized` embeds the user's optimized
    // experience document, and production runs EMBEDDINGS_PROVIDER=voyage.
    // The policy previously said "not your resume", which was false.
    const { container } = render(<PrivacyPage />);
    const text = container.textContent ?? '';
    expect(text).toMatch(/Voyage AI/);
    expect(text).toMatch(/extracts of your experience profile/i);
    expect(text).not.toMatch(/role labels, not your resume/i);
  });
});
