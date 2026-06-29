import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { axe, toHaveNoViolations } from 'jest-axe';
import CompletionScreen from '../CompletionScreen';

expect.extend(toHaveNoViolations);

const mockPush = jest.fn();

jest.mock('next/navigation', () => ({
  useRouter: () => ({ prefetch: jest.fn(), push: mockPush }),
}));

describe('CompletionScreen', () => {
  beforeEach(() => {
    mockPush.mockClear();
    global.fetch = jest.fn(() =>
      Promise.resolve({ ok: true, json: async () => ({}) })
    ) as jest.Mock;
  });

  it("renders the 'all set' heading", () => {
    render(<CompletionScreen />);
    expect(
      screen.getByRole('heading', { level: 2, name: /all set/i })
    ).toBeInTheDocument();
  });

  it('renders the supporting copy directing the user to targets', () => {
    render(<CompletionScreen />);
    expect(
      screen.getByText(/head to your targets to start tracking jobs/i)
    ).toBeInTheDocument();
  });

  it('renders the "Go to Targets" continue button', () => {
    render(<CompletionScreen />);
    expect(
      screen.getByRole('button', { name: /go to targets/i })
    ).toBeInTheDocument();
  });

  it('marks onboarding complete on click, then navigates to /targets', async () => {
    const user = userEvent.setup();
    render(<CompletionScreen />);

    await user.click(screen.getByRole('button', { name: /go to targets/i }));

    await waitFor(() => expect(mockPush).toHaveBeenCalledWith('/targets'));
    expect(global.fetch).toHaveBeenCalledWith(
      '/api/profile/onboarding/complete',
      expect.objectContaining({ method: 'POST' })
    );
  });

  it('does NOT navigate and shows an error when the complete call fails on every attempt', async () => {
    // fetch rejects on the initial call AND the retry → the write never
    // landed. Navigating anyway would drop the user into the dashboard's
    // redirect loop (NULL completed_at → bounced back). We surface a retry
    // affordance and stay put instead.
    (global.fetch as jest.Mock).mockRejectedValue(new Error('boom'));
    const user = userEvent.setup();
    render(<CompletionScreen />);

    await user.click(screen.getByRole('button', { name: /go to targets/i }));

    await waitFor(() =>
      expect(
        screen.getByText(/couldn.t finish setting up your account/i)
      ).toBeInTheDocument()
    );
    expect(mockPush).not.toHaveBeenCalled();
  });

  it('does NOT navigate on a non-2xx response (e.g. expired session → 401)', async () => {
    // A 401/503 *resolves* — fetch only rejects on network errors. The old
    // code swallowed it and navigated with the flag still NULL. We now
    // detect res.ok === false and block navigation.
    (global.fetch as jest.Mock).mockResolvedValue({
      ok: false,
      status: 401,
      json: async () => ({}),
    });
    const user = userEvent.setup();
    render(<CompletionScreen />);

    await user.click(screen.getByRole('button', { name: /go to targets/i }));

    await waitFor(() =>
      expect(
        screen.getByText(/couldn.t finish setting up your account/i)
      ).toBeInTheDocument()
    );
    expect(mockPush).not.toHaveBeenCalled();
  });

  it('navigates to /targets when a transient 5xx succeeds on retry', async () => {
    (global.fetch as jest.Mock)
      .mockResolvedValueOnce({ ok: false, status: 503, json: async () => ({}) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({}) });
    const user = userEvent.setup();
    render(<CompletionScreen />);

    await user.click(screen.getByRole('button', { name: /go to targets/i }));

    await waitFor(() => expect(mockPush).toHaveBeenCalledWith('/targets'));
  });

  it('has no accessibility violations', async () => {
    const { container } = render(<CompletionScreen />);
    expect(await axe(container)).toHaveNoViolations();
  });
});
