import React from 'react';
import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';
import StatusIndicator from '../StatusIndicator';
import { JOB_STATUSES, formatStatus } from '../types';

describe('StatusIndicator', () => {
  it('renders a known status with its formatted label', () => {
    render(<StatusIndicator status='resume_draft' />);
    expect(screen.getByText('Resume Draft')).toBeInTheDocument();
  });

  it('title-cases unknown statuses like known ones', () => {
    render(<StatusIndicator status='mystery' />);
    expect(screen.getByText('Mystery')).toBeInTheDocument();
  });

  it('renders one indicator per known JOB_STATUS', () => {
    for (const status of JOB_STATUSES) {
      const { unmount } = render(<StatusIndicator status={status} />);
      expect(screen.getByText(formatStatus(status))).toBeInTheDocument();
      unmount();
    }
  });
});
