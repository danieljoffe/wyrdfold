import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, fireEvent } from '@testing-library/react';
import Breadcrumbs, { crumbLabel } from '../Breadcrumbs';

const push = jest.fn();
jest.mock('next/navigation', () => ({
  useRouter: () => ({ push: (...a: unknown[]) => push(...a) }),
}));

beforeEach(() => jest.clearAllMocks());

describe('Breadcrumbs (kit)', () => {
  const items = [
    { label: 'Jobs', href: '/jobs' },
    { label: 'Senior Engineer' },
  ];

  test('renders the lib breadcrumb semantics (nav + aria-current on the leaf)', () => {
    render(<Breadcrumbs items={items} />);
    expect(
      screen.getByRole('navigation', { name: /breadcrumb/i })
    ).toBeInTheDocument();
    expect(screen.getByText('Senior Engineer')).toHaveAttribute(
      'aria-current',
      'page'
    );
    expect(screen.getByRole('link', { name: 'Jobs' })).toHaveAttribute(
      'href',
      '/jobs'
    );
  });

  test('intercepts internal-link clicks into client navigation', () => {
    render(<Breadcrumbs items={items} />);
    fireEvent.click(screen.getByRole('link', { name: 'Jobs' }));
    expect(push).toHaveBeenCalledWith('/jobs');
  });

  test('modified clicks pass through (open-in-new-tab preserved)', () => {
    render(<Breadcrumbs items={items} />);
    fireEvent.click(screen.getByRole('link', { name: 'Jobs' }), {
      metaKey: true,
    });
    expect(push).not.toHaveBeenCalled();
  });

  test('non-internal hrefs pass through untouched', () => {
    render(
      <Breadcrumbs
        items={[{ label: 'Docs', href: 'https://example.com' }, { label: 'X' }]}
      />
    );
    fireEvent.click(screen.getByRole('link', { name: 'Docs' }));
    expect(push).not.toHaveBeenCalled();
  });
});

describe('crumbLabel', () => {
  test('passes short labels through', () => {
    expect(crumbLabel('Senior Engineer')).toBe('Senior Engineer');
  });
  test('caps long labels with an ellipsis at the limit', () => {
    const long = 'x'.repeat(100);
    const out = crumbLabel(long);
    expect(out).toHaveLength(60);
    expect(out.endsWith('…')).toBe(true);
  });
});
