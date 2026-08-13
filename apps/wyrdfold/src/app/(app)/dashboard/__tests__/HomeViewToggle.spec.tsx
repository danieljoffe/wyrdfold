import React from 'react';
import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import HomeViewToggle from '../HomeViewToggle';

const replace = jest.fn();
jest.mock('next/navigation', () => ({
  usePathname: () => '/dashboard',
  useRouter: () => ({ replace }),
  useSearchParams: () => new URLSearchParams(),
}));

/**
 * Optimistic selected state (#605 / evidence in #601): the highlight used
 * to derive purely from the server-committed ``value`` prop, so it lagged
 * the click by the full round-trip (~4s measured in prod) — the control
 * read as dead. It must flip immediately on click.
 */
describe('HomeViewToggle', () => {
  beforeEach(() => replace.mockClear());

  it('flips the pressed state immediately on click, before any server commit', async () => {
    const user = userEvent.setup();
    render(<HomeViewToggle value='today' />);

    expect(screen.getByRole('button', { name: 'Overview' })).toHaveAttribute(
      'aria-pressed',
      'true'
    );

    await user.click(screen.getByRole('button', { name: 'Trends' }));

    // The prop is still 'today' (no rerender from a server commit), but
    // the optimistic state must already show Trends as active.
    expect(screen.getByRole('button', { name: 'Trends' })).toHaveAttribute(
      'aria-pressed',
      'true'
    );
    expect(screen.getByRole('button', { name: 'Overview' })).toHaveAttribute(
      'aria-pressed',
      'false'
    );
    expect(replace).toHaveBeenCalledWith('/dashboard?view=trends', {
      scroll: false,
    });
  });

  it('clears the view param when returning to Overview', async () => {
    const user = userEvent.setup();
    render(<HomeViewToggle value='trends' />);

    await user.click(screen.getByRole('button', { name: 'Overview' }));

    expect(screen.getByRole('button', { name: 'Overview' })).toHaveAttribute(
      'aria-pressed',
      'true'
    );
    expect(replace).toHaveBeenCalledWith('/dashboard', { scroll: false });
  });
});
