import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import TargetsList from '../TargetsList';
import {
  emptyScoringProfile,
  toSummary,
  type JobTarget,
  type UserTarget,
  type UserTargetWithSummary,
} from '../types';

/**
 * The delete prompt must name what it is about to destroy.
 *
 * It used to read only "Delete target?" — unverifiable on an account holding
 * several near-identical labels, which is the normal shape of a real target
 * list ("Senior Full Stack Engineer" vs "Senior Full-Stack Engineer" vs
 * "Staff Full-Stack Engineer"). Opening the wrong card's kebab was both
 * undetectable and unrecoverable: the action is irreversible by design, which
 * is precisely why the name belongs in the prompt.
 */

const mockPush = jest.fn();
const mockRefresh = jest.fn();
jest.mock('next/navigation', () => ({
  useRouter: () => ({
    push: mockPush,
    prefetch: jest.fn(),
    refresh: mockRefresh,
  }),
}));

const mockToast = jest.fn();
jest.mock('@/state/Toast/ToastProvider', () => ({
  useToast: () => ({ toast: mockToast }),
}));

jest.mock('../CreateTargetModal', () => ({
  __esModule: true,
  default: () => null,
}));

const originalFetch = global.fetch;
afterEach(() => {
  global.fetch = originalFetch;
  mockToast.mockReset();
  mockPush.mockReset();
  mockRefresh.mockReset();
});

function entry(id: string, label: string): UserTargetWithSummary {
  const target: JobTarget = {
    id,
    label,
    description: null,
    normalized_label: null,
    scoring_profile: { ...emptyScoringProfile() },
    search_keywords: [],
    activation_status: 'ready',
    profile_version: 1,
    app_active: true,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-04-30T00:00:00Z',
  };
  const userTarget: UserTarget = {
    id: `u-${id}`,
    user_id: 'user',
    target_id: id,
    is_active: false,
    fit_score: 80,
    fit_score_reasoning: null,
    axis_weights: null,
    axis_weights_previous: null,
    job_score_threshold: null,
    sms_score_threshold: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-04-30T00:00:00Z',
  };
  return { user_target: userTarget, target: toSummary(target) };
}

// The realistic hazard: labels that differ by a hyphen and a word.
const NEAR_IDENTICAL = [
  entry('t-1', 'Senior Full Stack Engineer'),
  entry('t-2', 'Senior Full-Stack Engineer'),
  entry('t-3', 'Staff Full-Stack Engineer'),
];

/** Open the kebab for a card and click its Delete item. */
async function openDeleteFor(
  user: ReturnType<typeof userEvent.setup>,
  label: string
) {
  const card = screen
    .getByRole('button', { name: `Open target ${label}` })
    .closest('.rounded-lg') as HTMLElement;
  const kebab = card.querySelector(
    'button[aria-haspopup="menu"]'
  ) as HTMLButtonElement;
  await user.click(kebab);
  await user.click(await screen.findByRole('menuitem', { name: /delete/i }));
}

describe('TargetsList — delete confirmation', () => {
  it('names the target being deleted', async () => {
    const user = userEvent.setup();
    render(<TargetsList initialTargets={NEAR_IDENTICAL} />);

    await openDeleteFor(user, 'Senior Full-Stack Engineer');

    // The exact one, not one of its two lookalikes.
    expect(
      await screen.findByText(/Delete “Senior Full-Stack Engineer”\?/)
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/Delete “Senior Full Stack Engineer”\?/)
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText(/Delete “Staff Full-Stack Engineer”\?/)
    ).not.toBeInTheDocument();
  });

  it('names a different target when a different card is chosen', async () => {
    const user = userEvent.setup();
    render(<TargetsList initialTargets={NEAR_IDENTICAL} />);

    await openDeleteFor(user, 'Staff Full-Stack Engineer');

    expect(
      await screen.findByText(/Delete “Staff Full-Stack Engineer”\?/)
    ).toBeInTheDocument();
  });

  it('keeps the irreversibility warning', async () => {
    const user = userEvent.setup();
    render(<TargetsList initialTargets={NEAR_IDENTICAL} />);

    await openDeleteFor(user, 'Senior Full Stack Engineer');

    expect(
      await screen.findByText(/This cannot be undone/i)
    ).toBeInTheDocument();
  });

  it('still deletes the target the prompt named', async () => {
    const fetchMock = jest.fn().mockResolvedValue({ ok: true });
    global.fetch = fetchMock as unknown as typeof fetch;

    const user = userEvent.setup();
    render(<TargetsList initialTargets={NEAR_IDENTICAL} />);

    await openDeleteFor(user, 'Senior Full-Stack Engineer');
    await screen.findByText(/Delete “Senior Full-Stack Engineer”\?/);
    await user.click(screen.getByRole('button', { name: /^delete$/i }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/targets/t-2',
        expect.objectContaining({ method: 'DELETE' })
      );
    });
  });
});
