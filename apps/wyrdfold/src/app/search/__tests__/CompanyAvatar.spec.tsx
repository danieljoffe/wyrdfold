import { fireEvent, render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';

import { CompanyAvatar, logoUrlTiers } from '../JobSearchExplorer';

// #470: logo links cascade Brandfetch (env-gated) → DuckDuckGo favicon →
// Google favicon → the initials monogram. Links only; an image error
// advances the tier. Note the cascade only rescues domains whose logo
// requests FAIL — a wrong-but-live domain (see the enrichment module's
// weak-verification caveat) renders the wrong company's logo instead.

describe('logoUrlTiers', () => {
  const origEnv = process.env.NEXT_PUBLIC_BRANDFETCH_CLIENT_ID;
  afterEach(() => {
    process.env.NEXT_PUBLIC_BRANDFETCH_CLIENT_ID = origEnv;
  });

  it('leads with Brandfetch only when a client id is configured', () => {
    process.env.NEXT_PUBLIC_BRANDFETCH_CLIENT_ID = 'cid-123';
    expect(logoUrlTiers('datadoghq.com')[0]).toBe(
      'https://cdn.brandfetch.io/datadoghq.com?c=cid-123'
    );
  });

  it('without a client id, starts at the token-free favicon tiers', () => {
    delete process.env.NEXT_PUBLIC_BRANDFETCH_CLIENT_ID;
    expect(logoUrlTiers('datadoghq.com')).toEqual([
      'https://icons.duckduckgo.com/ip3/datadoghq.com.ico',
    ]);
  });
});

describe('CompanyAvatar cascade', () => {
  beforeEach(() => {
    delete process.env.NEXT_PUBLIC_BRANDFETCH_CLIENT_ID;
  });

  it('renders the initials monogram when no domain is stored', () => {
    render(<CompanyAvatar name='Datadog' />);
    expect(screen.getByText('DA')).toBeInTheDocument();
    expect(document.querySelector('img')).toBeNull();
  });

  it('renders the first logo tier for an enriched company', () => {
    render(<CompanyAvatar name='Datadog' domain='datadoghq.com' />);
    const img = document.querySelector('img');
    expect(img).toHaveAttribute(
      'src',
      'https://icons.duckduckgo.com/ip3/datadoghq.com.ico'
    );
  });

  it('advances a tier per image error and lands on initials at the end', () => {
    render(<CompanyAvatar name='Datadog' domain='datadoghq.com' />);
    fireEvent.error(document.querySelector('img')!);
    expect(document.querySelector('img')).toBeNull();
    expect(screen.getByText('DA')).toBeInTheDocument();
  });
});

describe('cascade resets when the company changes (#470 review)', () => {
  beforeEach(() => {
    delete process.env.NEXT_PUBLIC_BRANDFETCH_CLIENT_ID;
  });

  it('restarts at the first tier when the domain prop changes', () => {
    // The tier lives in component state; React reuses that state when the
    // same element position receives new props. Without a remount keyed on
    // the domain, company B inherits company A's failed tier.
    const { rerender } = render(<CompanyAvatar name='A' domain='a.com' />);
    // Exhaust A's cascade so it is sitting on the initials fallback.
    fireEvent.error(document.querySelector('img')!);
    expect(document.querySelector('img')).toBeNull();

    rerender(<CompanyAvatar name='B' domain='b.com' />);

    expect(document.querySelector('img')).toHaveAttribute(
      'src',
      'https://icons.duckduckgo.com/ip3/b.com.ico'
    );
  });

  it('a company whose cascade was exhausted does not suppress the next one', () => {
    // The worse form of the same bug: A ran out of tiers and fell back to
    // initials, so B would render initials WITHOUT EVER REQUESTING a logo.
    const { rerender } = render(<CompanyAvatar name='A' domain='a.com' />);
    fireEvent.error(document.querySelector('img')!);
    expect(document.querySelector('img')).toBeNull();
    expect(screen.getByText('A')).toBeInTheDocument();

    rerender(<CompanyAvatar name='B' domain='b.com' />);

    expect(document.querySelector('img')).toHaveAttribute(
      'src',
      'https://icons.duckduckgo.com/ip3/b.com.ico'
    );
  });

  it('keeps the cascade position across unrelated re-renders of the same company', () => {
    // The reset must key on the DOMAIN, not fire on every render — a parent
    // re-render (hover, filter change) must not retry a tier already known
    // to fail, or a dead logo host gets re-requested on every keystroke.
    // With Brandfetch configured there are two tiers, so a failure has
    // somewhere to advance TO — the point of this test.
    process.env.NEXT_PUBLIC_BRANDFETCH_CLIENT_ID = 'cid-123';
    const { rerender } = render(<CompanyAvatar name='A' domain='a.com' />);
    fireEvent.error(document.querySelector('img')!);
    const advanced = 'https://icons.duckduckgo.com/ip3/a.com.ico';
    expect(document.querySelector('img')).toHaveAttribute('src', advanced);

    rerender(<CompanyAvatar name='A' domain='a.com' size='lg' />);

    expect(document.querySelector('img')).toHaveAttribute('src', advanced);
  });
});
