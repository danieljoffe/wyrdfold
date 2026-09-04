import { fireEvent, render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';

import { CompanyAvatar, logoUrlTiers } from '../JobSearchExplorer';

// #470: logo links cascade Brandfetch (env-gated) → DuckDuckGo favicon →
// Google favicon → the initials monogram. Links only; an image error
// advances the tier, so a wrong/parked domain degrades to exactly the
// pre-#470 rendering.

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
      'https://www.google.com/s2/favicons?domain=datadoghq.com&sz=64',
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
    expect(document.querySelector('img')).toHaveAttribute(
      'src',
      'https://www.google.com/s2/favicons?domain=datadoghq.com&sz=64'
    );
    fireEvent.error(document.querySelector('img')!);
    expect(document.querySelector('img')).toBeNull();
    expect(screen.getByText('DA')).toBeInTheDocument();
  });
});
