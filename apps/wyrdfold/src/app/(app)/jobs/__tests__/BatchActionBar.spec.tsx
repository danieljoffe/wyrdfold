import React from 'react';
import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import BatchActionBar from '../BatchActionBar';

const baseProps = {
  selectedCount: 0,
  onClear: jest.fn(),
  onBatchGenerate: jest.fn(),
  onBatchDelete: jest.fn(),
  onBatchExport: jest.fn(),
  onBatchStatus: jest.fn(),
  generating: false,
  exporting: false,
  statusUpdating: false,
  hasApproved: false,
};

beforeEach(() => {
  jest.clearAllMocks();
});

describe('BatchActionBar', () => {
  it('renders nothing when selectedCount is 0', () => {
    const { container } = render(<BatchActionBar {...baseProps} />);
    expect(container.firstChild).toBeNull();
  });

  it('renders the selection count and core actions when items are selected', () => {
    render(<BatchActionBar {...baseProps} selectedCount={2} />);
    expect(screen.getByText(/2 selected/i)).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /deselect/i })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /generate tailored resumes/i })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /^remove$/i })
    ).toBeInTheDocument();
  });

  it('hides the Export button until at least one approved resume is in the selection', () => {
    const { rerender } = render(
      <BatchActionBar {...baseProps} selectedCount={2} />
    );
    expect(
      screen.queryByRole('button', { name: /export/i })
    ).not.toBeInTheDocument();

    rerender(<BatchActionBar {...baseProps} selectedCount={2} hasApproved />);
    expect(screen.getByRole('button', { name: /export/i })).toBeInTheDocument();
  });

  it('shows a soft warning when the selection grows past the warn threshold', () => {
    render(<BatchActionBar {...baseProps} selectedCount={6} />);
    expect(screen.getByText(/large batch/i)).toBeInTheDocument();
  });

  it('disables Generate and shows a hard cap warning past the max', () => {
    render(<BatchActionBar {...baseProps} selectedCount={21} />);
    expect(
      screen.getByRole('button', { name: /generate tailored resumes/i })
    ).toBeDisabled();
    expect(screen.getByText(/max 20 per batch/i)).toBeInTheDocument();
  });

  it('reflects in-flight progress in the Generate label when batchProgress is set', () => {
    render(
      <BatchActionBar
        {...baseProps}
        selectedCount={5}
        generating
        batchProgress={{ completed: 2, total: 5 }}
      />
    );
    expect(
      screen.getByRole('button', { name: /generating 2 of 5/i })
    ).toBeDisabled();
  });

  it('invokes the action callbacks on click', async () => {
    const user = userEvent.setup();
    const onClear = jest.fn();
    const onBatchGenerate = jest.fn();
    const onBatchDelete = jest.fn();
    const onBatchExport = jest.fn();
    render(
      <BatchActionBar
        {...baseProps}
        selectedCount={2}
        hasApproved
        onClear={onClear}
        onBatchGenerate={onBatchGenerate}
        onBatchDelete={onBatchDelete}
        onBatchExport={onBatchExport}
      />
    );

    await user.click(screen.getByRole('button', { name: /deselect/i }));
    await user.click(
      screen.getByRole('button', { name: /generate tailored resumes/i })
    );
    await user.click(screen.getByRole('button', { name: /export/i }));
    await user.click(screen.getByRole('button', { name: /^remove$/i }));

    expect(onClear).toHaveBeenCalledTimes(1);
    expect(onBatchGenerate).toHaveBeenCalledTimes(1);
    expect(onBatchExport).toHaveBeenCalledTimes(1);
    expect(onBatchDelete).toHaveBeenCalledTimes(1);
  });
});

// #10: status was the one pipeline action with no bulk form — marking ten
// jobs "applied" meant opening each one in turn.
describe('BatchActionBar — bulk status (#10)', () => {
  it('offers every job status and reports the picked one', async () => {
    const onBatchStatus = jest.fn();
    const user = userEvent.setup();
    render(
      <BatchActionBar
        {...baseProps}
        selectedCount={3}
        onBatchStatus={onBatchStatus}
      />
    );

    await user.click(screen.getByText(/set status/i));
    // The full vocabulary, not a hand-picked subset — a bulk control that
    // can't reach a status the single-job menu can is its own trap.
    expect(screen.getByText('Applied')).toBeInTheDocument();
    expect(screen.getByText('Interviewing')).toBeInTheDocument();
    expect(screen.getByText('Rejected')).toBeInTheDocument();

    await user.click(screen.getByText('Applied'));
    expect(onBatchStatus).toHaveBeenCalledWith('applied');
  });

  it('shows progress and blocks re-entry while an update is in flight', () => {
    render(<BatchActionBar {...baseProps} selectedCount={3} statusUpdating />);
    expect(screen.getByText(/updating/i)).toBeInTheDocument();
    expect(screen.queryByText(/set status/i)).not.toBeInTheDocument();
  });
});
