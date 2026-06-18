import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import OnboardingResetCard from '../OnboardingResetCard';

const mockPush = jest.fn();
const mockToast = jest.fn();

jest.mock('next/navigation', () => ({
  useRouter: () => ({ push: mockPush, prefetch: jest.fn() }),
}));

jest.mock('@/state/Toast/ToastProvider', () => ({
  useToast: () => ({ toast: mockToast }),
}));

describe('OnboardingResetCard', () => {
  beforeEach(() => {
    mockPush.mockClear();
    mockToast.mockClear();
    global.fetch = jest.fn() as jest.Mock;
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('renders the heading + primary button', () => {
    render(<OnboardingResetCard />);
    expect(
      screen.getByText(/profile, targets, and saved jobs/i)
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /Redo onboarding/i })
    ).toBeInTheDocument();
  });

  it('opens the confirm modal, then POSTs reset → routes to /onboarding on confirm', async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce({ ok: true });
    const user = userEvent.setup();
    render(<OnboardingResetCard />);

    // Clicking the trigger only opens the dialog — no reset yet.
    await user.click(screen.getByRole('button', { name: /Redo onboarding/i }));
    expect(global.fetch).not.toHaveBeenCalled();

    // Confirm inside the dialog.
    const dialog = await screen.findByRole('dialog');
    await user.click(
      within(dialog).getByRole('button', { name: /^Redo onboarding$/i })
    );

    await waitFor(() =>
      expect(global.fetch).toHaveBeenCalledWith(
        '/api/profile/onboarding/reset',
        expect.objectContaining({ method: 'POST' })
      )
    );
    expect(mockPush).toHaveBeenCalledWith('/onboarding');
    expect(mockToast).not.toHaveBeenCalled();
  });

  it('aborts when the user cancels the confirm dialog', async () => {
    const user = userEvent.setup();
    render(<OnboardingResetCard />);

    await user.click(screen.getByRole('button', { name: /Redo onboarding/i }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: /cancel/i }));

    expect(global.fetch).not.toHaveBeenCalled();
    expect(mockPush).not.toHaveBeenCalled();
  });

  it('toasts an error and stays on the page when the server fails', async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce({
      ok: false,
      status: 500,
    });
    const user = userEvent.setup();
    render(<OnboardingResetCard />);

    await user.click(screen.getByRole('button', { name: /Redo onboarding/i }));
    const dialog = await screen.findByRole('dialog');
    await user.click(
      within(dialog).getByRole('button', { name: /^Redo onboarding$/i })
    );

    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: 'error' })
      )
    );
    expect(mockPush).not.toHaveBeenCalled();
  });
});
