import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import IdentityStep from '../IdentityStep';

const fetchMock = jest.fn();
global.fetch = fetchMock as unknown as typeof fetch;

beforeEach(() => {
  fetchMock.mockReset();
});

describe('IdentityStep', () => {
  it('auto-advances when a name is already on file (re-collection dedup)', async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ name: 'Daniel', email: 'me@example.com' }),
    });
    const onComplete = jest.fn();

    render(<IdentityStep onComplete={onComplete} onSkip={jest.fn()} />);

    await waitFor(() => expect(onComplete).toHaveBeenCalled());
  });

  it('renders the form when no name is on file, prefilled with the sign-in email', async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ name: null, email: 'me@example.com' }),
    });
    const onComplete = jest.fn();

    render(<IdentityStep onComplete={onComplete} onSkip={jest.fn()} />);

    await waitFor(() =>
      expect(screen.getByLabelText(/^Email/)).toHaveValue('me@example.com')
    );
    expect(screen.getByLabelText(/^Name/)).toHaveValue('');
    expect(onComplete).not.toHaveBeenCalled();
  });

  it('still renders the form when the identity GET fails', async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 500 });
    const onComplete = jest.fn();

    render(<IdentityStep onComplete={onComplete} onSkip={jest.fn()} />);

    expect(await screen.findByLabelText(/^Name/)).toBeInTheDocument();
    expect(onComplete).not.toHaveBeenCalled();
  });
});
