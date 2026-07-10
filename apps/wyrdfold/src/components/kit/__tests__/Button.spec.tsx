import React from 'react';
import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { axe, toHaveNoViolations } from 'jest-axe';
import Button from '../Button';

expect.extend(toHaveNoViolations);

describe('kit Button (shared-ui shim)', () => {
  test('renders a native button', () => {
    render(<Button name='testing'>Click me</Button>);
    expect(
      screen.getByRole('button', { name: /click me/i })
    ).toBeInTheDocument();
  });

  test('respects provided button type', () => {
    render(
      <Button name='testing' type='submit'>
        Submit
      </Button>
    );
    expect(screen.getByRole('button', { name: /submit/i })).toHaveAttribute(
      'type',
      'submit'
    );
  });

  test('disabled button has disabled attribute and does not trigger onClick', async () => {
    const user = userEvent.setup();
    const onClick = jest.fn();
    render(
      <Button name='testing' disabled onClick={onClick}>
        Disabled
      </Button>
    );
    const button = screen.getByRole('button', { name: /disabled/i });
    expect(button).toBeDisabled();
    await user.click(button);
    expect(onClick).not.toHaveBeenCalled();
  });

  test('loading disables the button and sets aria-busy', () => {
    render(
      <Button name='testing' loading>
        Saving
      </Button>
    );
    const button = screen.getByRole('button', { name: /saving/i });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('aria-busy', 'true');
  });

  // Phase 4a finding #7 — the shim must override the lib's
  // `disabled:opacity-50` with the neutral surface treatment so a disabled
  // primary CTA doesn't read as an error state on pyre's chartreuse.
  test('applies the neutral disabled treatment over the lib default', () => {
    render(
      <Button name='testing' disabled>
        Disabled
      </Button>
    );
    const button = screen.getByRole('button', { name: /disabled/i });
    expect(button.className).toMatch(/\bdisabled:opacity-100\b/);
    expect(button.className).toMatch(/\bdisabled:bg-surface-elevated\b/);
    expect(button.className).toMatch(/\bdisabled:text-text-tertiary\b/);
    // tailwind-merge must have dropped the lib's opacity-50 in favor of ours.
    expect(button.className).not.toMatch(/\bdisabled:opacity-50\b/);
  });

  test('handles keyboard Enter key on button', async () => {
    const user = userEvent.setup();
    const onClick = jest.fn();
    render(
      <Button name='testing' onClick={onClick}>
        Press
      </Button>
    );
    screen.getByRole('button', { name: /press/i }).focus();
    await user.keyboard('{Enter}');
    expect(onClick).toHaveBeenCalled();
  });

  test('does not fire onClick on keyboard when disabled', async () => {
    const user = userEvent.setup();
    const onClick = jest.fn();
    render(
      <Button name='testing' disabled onClick={onClick}>
        Disabled
      </Button>
    );
    screen.getByRole('button', { name: /disabled/i }).focus();
    await user.keyboard('{Enter}');
    expect(onClick).not.toHaveBeenCalled();
  });

  // #25 F2 — icon-only buttons must meet the 44×44 minimum hit area
  // regardless of size. The lib's iconOnly paddings alone don't guarantee
  // it; the shim injects min-h-11/min-w-11. Pins the contract.
  test.each<['sm' | 'md' | 'lg']>([['sm'], ['md'], ['lg']])(
    'iconOnly size %s enforces a 44×44 minimum hit area',
    size => {
      render(
        <Button name='close' iconOnly size={size} aria-label='Close'>
          <span aria-hidden>×</span>
        </Button>
      );
      const button = screen.getByRole('button', { name: /close/i });
      expect(button.className).toMatch(/\bmin-h-11\b/);
      expect(button.className).toMatch(/\bmin-w-11\b/);
    }
  );

  it('has no accessibility violations', async () => {
    const { container } = render(<Button name='test'>Click</Button>);
    expect(await axe(container)).toHaveNoViolations();
  });
});
