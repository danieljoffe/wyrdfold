import React from 'react';
import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';
import { axe, toHaveNoViolations } from 'jest-axe';
import WyrdfoldWordmark from '../WyrdfoldWordmark';

expect.extend(toHaveNoViolations);

describe('WyrdfoldWordmark', () => {
  it('renders an accessible img with default aria-label "WyrdFold"', () => {
    render(<WyrdfoldWordmark />);
    expect(screen.getByRole('img', { name: 'WyrdFold' })).toBeInTheDocument();
  });

  it('respects a custom aria-label', () => {
    render(<WyrdfoldWordmark aria-label='WyrdFold home' />);
    expect(
      screen.getByRole('img', { name: 'WyrdFold home' })
    ).toBeInTheDocument();
  });

  it('renders as decorative (presentation) when aria-hidden is true', () => {
    const { container } = render(<WyrdfoldWordmark aria-hidden />);
    expect(screen.queryByRole('img')).toBeNull();
    const svg = container.querySelector('svg');
    expect(svg).not.toBeNull();
    expect(svg).toHaveAttribute('aria-hidden', 'true');
    expect(svg).not.toHaveAttribute('aria-label');
    expect(svg?.querySelector('title')).toBeNull();
  });

  it('keeps the brand mark green and themes the wordmark via currentColor', () => {
    const { container } = render(<WyrdfoldWordmark />);
    const fills = Array.from(container.querySelectorAll('path')).map(p =>
      p.getAttribute('fill')
    );
    // 3 brand-green mark glyphs + 8 currentColor wordmark glyphs.
    expect(fills.filter(f => f === '#8FC900')).toHaveLength(3);
    expect(fills.filter(f => f === 'currentColor')).toHaveLength(8);
  });

  it('passes through className', () => {
    const { container } = render(<WyrdfoldWordmark className='custom-class' />);
    expect(container.querySelector('svg')?.getAttribute('class')).toContain(
      'custom-class'
    );
  });

  it('has no accessibility violations (visible variant)', async () => {
    const { container } = render(<WyrdfoldWordmark />);
    expect(await axe(container)).toHaveNoViolations();
  });

  it('has no accessibility violations (decorative inside a labelled link)', async () => {
    const { container } = render(
      <a href='/' aria-label='WyrdFold home'>
        <WyrdfoldWordmark aria-hidden />
      </a>
    );
    expect(await axe(container)).toHaveNoViolations();
  });
});
