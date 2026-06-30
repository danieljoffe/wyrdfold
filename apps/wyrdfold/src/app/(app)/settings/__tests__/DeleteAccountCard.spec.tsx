import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import DeleteAccountCard from '../DeleteAccountCard';

const mockReplace = jest.fn();
const mockRefresh = jest.fn();
const mockToast = jest.fn();
const mockSignOut = jest.fn();

jest.mock('next/navigation', () => ({
  useRouter: () => ({
    replace: mockReplace,
    refresh: mockRefresh,
    prefetch: jest.fn(),
  }),
}));

jest.mock('@/state/Toast/ToastProvider', () => ({
  useToast: () => ({ toast: mockToast }),
}));

jest.mock('@/lib/supabase/auth-client', () => ({
  createAuthBrowserClient: () => ({ auth: { signOut: mockSignOut } }),
}));

describe('DeleteAccountCard', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockSignOut.mockResolvedValue({ error: null });
    global.fetch = jest.fn() as jest.Mock;
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('renders the danger-zone copy + trigger', () => {
    render(<DeleteAccountCard />);
    expect(
      screen.getByText(/permanently delete your account/i)
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /delete my account/i })
    ).toBeInTheDocument();
  });

  it('opens the modal; the confirm stays disabled until the exact phrase is typed', async () => {
    const user = userEvent.setup();
    render(<DeleteAccountCard />);

    // Opening the dialog must not delete anything.
    await user.click(
      screen.getByRole('button', { name: /delete my account/i })
    );
    expect(global.fetch).not.toHaveBeenCalled();

    const dialog = await screen.findByRole('dialog');
    const confirm = within(dialog).getByRole('button', {
      name: /^Delete account$/i,
    });
    expect(confirm).toBeDisabled();

    // Wrong text keeps it disabled.
    await user.type(within(dialog).getByRole('textbox'), 'nope');
    expect(confirm).toBeDisabled();
  });

  it('DELETEs, signs out, and redirects to /login on the typed confirmation', async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ deleted: true }),
    });
    const user = userEvent.setup();
    render(<DeleteAccountCard />);

    await user.click(
      screen.getByRole('button', { name: /delete my account/i })
    );
    const dialog = await screen.findByRole('dialog');
    // Phrase match is case/space-insensitive.
    await user.type(
      within(dialog).getByRole('textbox'),
      '  Delete My Account '
    );
    const confirm = within(dialog).getByRole('button', {
      name: /^Delete account$/i,
    });
    expect(confirm).toBeEnabled();
    await user.click(confirm);

    await waitFor(() =>
      expect(global.fetch).toHaveBeenCalledWith(
        '/api/profile/account',
        expect.objectContaining({ method: 'DELETE' })
      )
    );
    await waitFor(() => expect(mockSignOut).toHaveBeenCalled());
    expect(mockReplace).toHaveBeenCalledWith('/login');
    expect(mockToast).not.toHaveBeenCalled();
  });

  it('toasts an error and stays put when deletion fails (no sign-out, no redirect)', async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce({
      ok: false,
      status: 500,
      clone: () => ({ json: async () => ({}) }),
      json: async () => ({}),
    });
    const user = userEvent.setup();
    render(<DeleteAccountCard />);

    await user.click(
      screen.getByRole('button', { name: /delete my account/i })
    );
    const dialog = await screen.findByRole('dialog');
    await user.type(within(dialog).getByRole('textbox'), 'delete my account');
    await user.click(
      within(dialog).getByRole('button', { name: /^Delete account$/i })
    );

    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: 'error' })
      )
    );
    expect(mockSignOut).not.toHaveBeenCalled();
    expect(mockReplace).not.toHaveBeenCalled();
  });
});
