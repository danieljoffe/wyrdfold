import React from 'react';
import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import LinkButton from '../LinkButton';

// Mock next/link to render a real <a>
jest.mock('next/link', () => {
  return function MockLink(
    props: React.AnchorHTMLAttributes<HTMLAnchorElement> & { href: string }
  ) {
    const { href, children, onClick, ...rest } = props;
    return (
      <a
        href={href}
        onClick={e => {
          e.preventDefault();
          onClick?.(e);
        }}
        {...rest}
      >
        {children}
      </a>
    );
  };
});

const prefetch = jest.fn();
jest.mock('next/navigation', () => ({
  useRouter: () => ({ prefetch: (...a: unknown[]) => prefetch(...a) }),
}));

beforeEach(() => jest.clearAllMocks());

describe('LinkButton (kit)', () => {
  test('renders a link with the href', () => {
    render(<LinkButton href='/test'>Go</LinkButton>);
    const link = screen.getByRole('link', { name: /go/i });
    expect(link).toHaveAttribute('href', '/test');
  });

  test('renders nothing without an href', () => {
    const { container } = render(
      // Deliberately exercising the runtime empty-href guard. An empty string
      // is a well-typed href, so this needs no type suppression.
      <LinkButton href=''>Go</LinkButton>
    );
    expect(container).toBeEmptyDOMElement();
  });

  test('adds rel="noopener noreferrer" when target="_blank"', () => {
    render(
      <LinkButton href='/ext' target='_blank'>
        External
      </LinkButton>
    );
    const link = screen.getByRole('link', { name: /external/i });
    expect(link.getAttribute('rel') ?? '').toEqual(
      expect.stringContaining('noopener')
    );
    expect(link.getAttribute('rel') ?? '').toEqual(
      expect.stringContaining('noreferrer')
    );
  });

  test('disabled renders a non-interactive span with aria-disabled', () => {
    render(
      <LinkButton href='/x' disabled>
        NoGo
      </LinkButton>
    );
    const pseudoLink = screen.getByRole('link', { name: /nogo/i });
    expect(pseudoLink.tagName).toBe('SPAN');
    expect(pseudoLink).toHaveAttribute('aria-disabled', 'true');
    expect(pseudoLink).not.toHaveAttribute('href');
  });

  test('fires onClick for an enabled link', async () => {
    const user = userEvent.setup();
    const onClick = jest.fn();
    render(
      <LinkButton href='/ok' onClick={onClick}>
        Go
      </LinkButton>
    );
    await user.click(screen.getByRole('link', { name: /go/i }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  test('hover does NOT imperatively prefetch — native next/link owns it', async () => {
    // The kit button once mirrored hover into router.prefetch(). Next's
    // app-router Link already prefetches on hover natively
    // (onNavigationIntent), so the imperative call only enqueued a duplicate
    // request per hover across every strip/row of LinkButtons.
    const user = userEvent.setup();
    render(<LinkButton href='/jobs/1'>Job</LinkButton>);
    await user.hover(screen.getByRole('link', { name: /job/i }));
    expect(prefetch).not.toHaveBeenCalled();
  });

  test('generates id from aria-label when id not provided', () => {
    render(
      <LinkButton href='/test' aria-label='Test Label'>
        Link
      </LinkButton>
    );
    expect(screen.getByRole('link', { name: /test label/i })).toHaveAttribute(
      'id',
      'Test-Label'
    );
  });

  test('uses provided id over aria-label', () => {
    render(
      <LinkButton href='/test' id='custom-id' aria-label='Test Label'>
        Link
      </LinkButton>
    );
    expect(screen.getByRole('link', { name: /test label/i })).toHaveAttribute(
      'id',
      'custom-id'
    );
  });

  test('applies variant and size styles from the shared-ui records', () => {
    render(
      <LinkButton href='/test' variant='secondary' size='lg'>
        Styled
      </LinkButton>
    );
    const link = screen.getByRole('link', { name: /styled/i });
    // Spot-check one class from each record so a lib restyle still passes
    // (we assert composition happens, not exact pixel classes).
    expect(link.className).toContain('inline-flex'); // base
    expect(link.className).toContain('text-lg'); // size lg
  });
});
