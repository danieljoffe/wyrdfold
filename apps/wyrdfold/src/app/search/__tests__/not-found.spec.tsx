import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';
import { expectNoA11yViolations } from '@/test-utils/axe';
import SearchNotFound from '../not-found';

// The behavioral half of #831 — logged-out visitors get the public header,
// not the member sidebar — comes from `search/layout.tsx`, which wraps this
// file per Next's segment not-found convention. This spec covers the
// content: a dead listing explains itself and routes back to live search.
describe('search/not-found', () => {
  it('explains the dead listing and offers the live search', () => {
    render(<SearchNotFound />);

    expect(
      screen.getByRole('heading', { level: 1, name: /listing not found/i })
    ).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /browse jobs/i })).toHaveAttribute(
      'href',
      '/search'
    );
  });

  it('does not render the member sidebar shell', () => {
    render(<SearchNotFound />);
    // The root not-found renders WyrdfoldSidebar (nav landmarks); this one
    // must not — the surrounding layout decides the shell (#831).
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument();
  });

  it('has no axe violations', async () => {
    const { container } = render(<SearchNotFound />);
    await expectNoA11yViolations(container);
  });
});
