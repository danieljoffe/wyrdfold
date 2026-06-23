import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';
import { expectNoA11yViolations } from '@/test-utils/axe';

jest.mock('next/navigation', () => ({
  useRouter: () => ({ push: jest.fn(), prefetch: jest.fn() }),
}));

import WyrdfoldLandingPage from '../page';

describe('WyrdfoldLandingPage', () => {
  it('renders the narrative sections in CTA-building order', () => {
    const { container } = render(<WyrdfoldLandingPage />);

    const markers = [
      'Now taking waitlist signups',
      'Watching roles across',
      'How it works',
      'Nothing invented',
      'What you get',
      'Stop chasing the search',
    ];
    const html = container.innerHTML;
    const positions = markers.map(m => html.indexOf(m));
    positions.forEach(p => expect(p).toBeGreaterThan(-1));
    const sorted = [...positions].sort((a, b) => a - b);
    expect(positions).toEqual(sorted);
  });

  it('has a valid heading outline (single h1, h2 sections, h3 sub-items)', () => {
    render(<WyrdfoldLandingPage />);
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
    // 4 section h2s: How it works, Nothing invented, What you get, closing CTA
    expect(screen.getAllByRole('heading', { level: 2 })).toHaveLength(4);
    // h3s: 4 steps + 3 payoff cards = 7
    expect(screen.getAllByRole('heading', { level: 3 })).toHaveLength(7);
  });

  it('keeps the trust copy and applies branded card padding (p-8)', () => {
    const { container } = render(<WyrdfoldLandingPage />);
    expect(
      screen.getByText(/WyrdFold never invents experience/i)
    ).toBeInTheDocument();
    // Card padding='lg' -> p-8 must be present on rendered cards
    expect(container.querySelector('.p-8')).not.toBeNull();
  });

  it('uses contrast-safe brand text on light surfaces (no bare brand-300/500)', () => {
    const { container } = render(<WyrdfoldLandingPage />);
    const html = container.innerHTML;
    // Flag bare light-mode brand-300/500 *text* (not preceded by `dark:`),
    // which fails axe color-contrast on the light background.
    const bareBrandText = /(?<!dark:)text-brand-(?:300|500)\b/g;
    const matches = html.match(bareBrandText) ?? [];
    expect(matches).toEqual([]);
  });

  it('has no jest-axe a11y violations', async () => {
    const { container } = render(<WyrdfoldLandingPage />);
    // Mirror the e2e landing-page config: shared-ui Alert renders its title
    // as an <h5>, which trips heading-order; that's third-party markup.
    await expectNoA11yViolations(container, {
      disableRules: ['heading-order'],
    });
  });
});
