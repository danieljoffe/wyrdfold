import React from 'react';
import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';
import CircleBadge from '../CircleBadge';

describe('CircleBadge', () => {
  it('renders its content as a circle (rounded-full, square, no horizontal padding)', () => {
    render(<CircleBadge ariaLabel='Weight 3'>3</CircleBadge>);
    const chip = screen.getByText('3');
    expect(chip).toHaveClass('rounded-full');
    expect(chip).toHaveClass('aspect-square');
    expect(chip).toHaveClass('p-0');
    expect(chip).not.toHaveClass('rounded-md');
  });

  it('supports non-numeric content (e.g. a percentage) and an accessible name', () => {
    render(
      <CircleBadge ariaLabel='Document health 85%' variant='success'>
        85%
      </CircleBadge>
    );
    expect(screen.getByLabelText('Document health 85%')).toHaveTextContent(
      '85%'
    );
  });
});
