import React from 'react';
import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';

import LogisticsChips from '../LogisticsChips';
import type { LogisticsFilters } from '../types';

function logistics(
  overrides: Partial<LogisticsFilters> = {}
): LogisticsFilters {
  return {
    remote_status: 'unspecified',
    salary_min: null,
    salary_max: null,
    salary_currency: null,
    salary_unit: null,
    location_city: null,
    location_country: null,
    ...overrides,
  };
}

describe('LogisticsChips', () => {
  it('renders a Remote chip for remote_status "remote"', () => {
    render(<LogisticsChips filters={logistics({ remote_status: 'remote' })} />);
    expect(screen.getByText('Remote')).toBeInTheDocument();
  });

  it('labels hybrid and on-site', () => {
    const { rerender } = render(
      <LogisticsChips filters={logistics({ remote_status: 'hybrid' })} />
    );
    expect(screen.getByText('Hybrid')).toBeInTheDocument();
    rerender(
      <LogisticsChips filters={logistics({ remote_status: 'onsite' })} />
    );
    expect(screen.getByText('On-site')).toBeInTheDocument();
  });

  it('omits the remote chip when unspecified', () => {
    // unspecified + no other signal → the whole component renders nothing.
    const { container } = render(
      <LogisticsChips filters={logistics({ remote_status: 'unspecified' })} />
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('formats an annual salary range compactly ($k)', () => {
    render(
      <LogisticsChips
        filters={logistics({
          salary_min: 150000,
          salary_max: 185000,
          salary_currency: 'USD',
          salary_unit: 'year',
        })}
      />
    );
    expect(screen.getByText('$150k–$185k')).toBeInTheDocument();
  });

  it('formats a salary floor (min only) with a trailing +', () => {
    render(
      <LogisticsChips
        filters={logistics({ salary_min: 150000, salary_unit: 'year' })}
      />
    );
    expect(screen.getByText('$150k+')).toBeInTheDocument();
  });

  it('formats an hourly rate as-is with /hr', () => {
    render(
      <LogisticsChips
        filters={logistics({
          salary_min: 75,
          salary_max: 75,
          salary_unit: 'hour',
        })}
      />
    );
    expect(screen.getByText('$75/hr')).toBeInTheDocument();
  });

  it('renders a non-USD currency prefix', () => {
    render(
      <LogisticsChips
        filters={logistics({
          salary_min: 90000,
          salary_max: 90000,
          salary_currency: 'EUR',
          salary_unit: 'year',
        })}
      />
    );
    expect(screen.getByText('EUR 90k')).toBeInTheDocument();
  });

  it('formats city + country location', () => {
    render(
      <LogisticsChips
        filters={logistics({
          location_city: 'San Francisco',
          location_country: 'US',
        })}
      />
    );
    expect(screen.getByText('San Francisco, US')).toBeInTheDocument();
  });

  it('shows country alone when there is no city', () => {
    render(<LogisticsChips filters={logistics({ location_country: 'US' })} />);
    expect(screen.getByText('US')).toBeInTheDocument();
  });

  it('renders all three chips together', () => {
    render(
      <LogisticsChips
        filters={logistics({
          remote_status: 'remote',
          salary_min: 150000,
          salary_max: 185000,
          salary_unit: 'year',
          location_city: 'Austin',
          location_country: 'US',
        })}
      />
    );
    expect(screen.getByText('Remote')).toBeInTheDocument();
    expect(screen.getByText('$150k–$185k')).toBeInTheDocument();
    expect(screen.getByText('Austin, US')).toBeInTheDocument();
    expect(screen.getByLabelText('Job logistics')).toBeInTheDocument();
  });

  it('renders nothing when there is no signal at all', () => {
    const { container } = render(<LogisticsChips filters={logistics()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing when filters is null or undefined', () => {
    const { container, rerender } = render(<LogisticsChips filters={null} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<LogisticsChips filters={undefined} />);
    expect(container).toBeEmptyDOMElement();
  });
});
