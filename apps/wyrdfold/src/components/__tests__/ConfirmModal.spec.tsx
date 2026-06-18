import React from 'react';
import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ConfirmModal from '../ConfirmModal';

describe('ConfirmModal', () => {
  it('renders title + message and the confirm/cancel actions when open', () => {
    render(
      <ConfirmModal
        isOpen
        onClose={jest.fn()}
        onConfirm={jest.fn()}
        title='Delete posting?'
        message='This cannot be undone.'
        confirmLabel='Delete'
      />
    );
    expect(screen.getByText('Delete posting?')).toBeInTheDocument();
    expect(screen.getByText('This cannot be undone.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^delete$/i })).toBeEnabled();
    expect(screen.getByRole('button', { name: /^cancel$/i })).toBeEnabled();
  });

  it('renders nothing when closed', () => {
    render(
      <ConfirmModal
        isOpen={false}
        onClose={jest.fn()}
        onConfirm={jest.fn()}
        title='Delete posting?'
      />
    );
    expect(screen.queryByText('Delete posting?')).not.toBeInTheDocument();
  });

  it('calls onConfirm when the confirm button is clicked', async () => {
    const onConfirm = jest.fn();
    const user = userEvent.setup();
    render(
      <ConfirmModal
        isOpen
        onClose={jest.fn()}
        onConfirm={onConfirm}
        title='Delete?'
        confirmLabel='Delete'
      />
    );
    await user.click(screen.getByRole('button', { name: /^delete$/i }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it('calls onClose when the cancel button is clicked', async () => {
    const onClose = jest.fn();
    const user = userEvent.setup();
    render(
      <ConfirmModal
        isOpen
        onClose={onClose}
        onConfirm={jest.fn()}
        title='Delete?'
      />
    );
    await user.click(screen.getByRole('button', { name: /^cancel$/i }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('disables both buttons and shows the loading label while loading', () => {
    render(
      <ConfirmModal
        isOpen
        onClose={jest.fn()}
        onConfirm={jest.fn()}
        title='Delete?'
        confirmLabel='Delete'
        loading
        loadingLabel='Deleting…'
      />
    );
    expect(screen.getByText('Deleting…')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /deleting/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /^cancel$/i })).toBeDisabled();
  });
});
