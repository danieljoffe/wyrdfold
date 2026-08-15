import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import PausedTargetNotice from '../PausedTargetNotice';

/**
 * The recourse half of the paused-target fix. `resolveRequestedTarget` decides
 * that the user asked for a paused target; this is what they get told and what
 * they can do about it.
 */

const mockToast = jest.fn();
jest.mock('@/state/Toast/ToastProvider', () => ({
  useToast: () => ({ toast: mockToast }),
}));

const mockPush = jest.fn();
const mockRefresh = jest.fn();
jest.mock('next/navigation', () => ({
  useRouter: () => ({
    push: mockPush,
    prefetch: jest.fn(),
    refresh: mockRefresh,
  }),
}));

const ORIGINAL_FETCH = global.fetch;

afterEach(() => {
  global.fetch = ORIGINAL_FETCH;
  mockToast.mockReset();
  mockPush.mockReset();
  mockRefresh.mockReset();
});

describe('PausedTargetNotice', () => {
  it('names the target the user actually asked for', () => {
    render(
      <PausedTargetNotice targetId='t-2' label='Senior Backend Engineer' />
    );

    // Naming it is the entire point — the old behaviour showed another
    // target's jobs without ever mentioning the one that was clicked.
    const heading = screen.getByRole('heading', { name: /is paused/i });
    expect(heading).toHaveTextContent('Senior Backend Engineer');

    // ...and the action names it too, so the button is unambiguous when the
    // notice sits above a list of some other target's jobs.
    expect(
      screen.getByRole('button', { name: /resume .*senior backend engineer/i })
    ).toBeInTheDocument();
  });

  it('explains why there are no jobs for it', () => {
    render(
      <PausedTargetNotice targetId='t-2' label='Senior Backend Engineer' />
    );
    expect(
      screen.getByText(/aren’t matched against new postings/i)
    ).toBeInTheDocument();
  });

  it('resumes the target and refreshes on success', async () => {
    const fetchMock = jest.fn().mockResolvedValue({ ok: true });
    global.fetch = fetchMock as unknown as typeof fetch;

    const user = userEvent.setup();
    render(<PausedTargetNotice targetId='t-2' label='Backend' />);

    await user.click(screen.getByRole('button', { name: /resume/i }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/targets/t-2/activate',
        expect.objectContaining({ method: 'POST' })
      );
    });
    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: 'success' })
      );
    });
    expect(mockRefresh).toHaveBeenCalled();
  });

  it('surfaces a failed resume and does not refresh', async () => {
    const fetchMock = jest.fn().mockResolvedValue({
      ok: false,
      status: 409,
      clone: () => ({
        json: async () => ({ detail: 'You already have 1 active target.' }),
      }),
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    const user = userEvent.setup();
    render(<PausedTargetNotice targetId='t-2' label='Backend' />);

    await user.click(screen.getByRole('button', { name: /resume/i }));

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          variant: 'error',
          title: 'You already have 1 active target.',
        })
      );
    });
    expect(mockRefresh).not.toHaveBeenCalled();
  });

  it('offers a route to /targets as a second way out', async () => {
    const user = userEvent.setup();
    render(<PausedTargetNotice targetId='t-2' label='Backend' />);

    await user.click(screen.getByRole('button', { name: /manage targets/i }));
    expect(mockPush).toHaveBeenCalledWith('/targets');
  });
});
