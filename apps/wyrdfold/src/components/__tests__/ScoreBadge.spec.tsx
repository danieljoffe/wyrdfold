import React from 'react';
import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';
import ScoreBadge from '../ScoreBadge';

describe('ScoreBadge', () => {
  it('renders the score as a circular chip (rounded-full, square, no horizontal padding)', () => {
    render(<ScoreBadge score={87} />);
    const badge = screen.getByText('87');
    expect(badge).toHaveClass('rounded-full');
    expect(badge).toHaveClass('aspect-square');
    expect(badge).toHaveClass('p-0');
    // No leftover pill rounding from the base Badge.
    expect(badge).not.toHaveClass('rounded-md');
  });

  it('exposes the score via an accessible name', () => {
    render(<ScoreBadge score={42} />);
    expect(screen.getByLabelText('Match score 42')).toBeInTheDocument();
  });

  it('renders a scoring spinner only while scoring is in flight', () => {
    const { rerender } = render(
      <ScoreBadge score={50} scoringStatus='scoring' />
    );
    expect(screen.getByLabelText(/scoring in progress/i)).toBeInTheDocument();

    rerender(<ScoreBadge score={50} scoringStatus='complete' />);
    expect(screen.queryByLabelText(/scoring in progress/i)).toBeNull();

    rerender(<ScoreBadge score={50} />);
    expect(screen.queryByLabelText(/scoring in progress/i)).toBeNull();
  });
});
