import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import LearningLogPanel from '../LearningLogPanel';
import type { TargetLearningLogRow } from '../../types';

const mockToast = jest.fn();

jest.mock('@/state/Toast/ToastProvider', () => ({
  useToast: () => ({ toast: mockToast }),
}));

const stagedRow: TargetLearningLogRow = {
  id: 'log-staged',
  user_id: 'u1',
  target_id: 't1',
  status: 'staged',
  prev_profile: {},
  next_profile: {},
  diff: {
    add_negative: ['recruiting agency'],
    remove_negative: [],
    add_secondary: { rust: 2 },
    demote_keywords: [],
    confidence: 0.42,
    rationale: 'You marked 3 agency posts irrelevant.',
  },
  confidence: 0.42,
  rationale: 'You marked 3 agency posts irrelevant.',
  signals_consumed: 3,
  applied_run_id: null,
  projection: {
    jobs_considered: 20,
    jobs_moved: 4,
    moved_fraction: 0.2,
    max_abs_delta: 12,
    move_threshold: 5,
    max_moved_fraction: 0.5,
    capped: false,
  },
  created_at: '2026-06-20T10:00:00Z',
  updated_at: '2026-06-20T10:00:00Z',
};

const appliedRow: TargetLearningLogRow = {
  ...stagedRow,
  id: 'log-applied',
  status: 'applied',
  diff: { ...stagedRow.diff, add_negative: [], add_secondary: { golang: 3 } },
  rationale: 'Promoted golang from positive feedback.',
  applied_run_id: 'run-x',
};

/** Build a Response-ish stub for a successful JSON fetch. */
function ok(json: unknown) {
  return { ok: true, json: async () => json };
}
/** A failing Response stub that satisfies extractApiError's `clone().json()`. */
function fail(status: number) {
  return {
    ok: false,
    status,
    clone: () => ({ json: async () => ({}) }),
    json: async () => ({}),
  };
}

describe('LearningLogPanel', () => {
  beforeEach(() => {
    mockToast.mockClear();
    global.fetch = jest.fn() as jest.Mock;
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('renders the empty state once the (empty) log loads', async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce(ok([]));
    render(<LearningLogPanel targetId='t1' onProfileChanged={jest.fn()} />);

    expect(
      await screen.findByText(/No learning activity yet/i)
    ).toBeInTheDocument();
    expect(global.fetch).toHaveBeenCalledWith(
      '/api/targets/t1/learning-log?limit=50'
    );
    expect(
      screen.getByRole('button', { name: /check for updates/i })
    ).toBeInTheDocument();
  });

  it('renders a staged patch (diff chips, confidence, projection) + history', async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce(
      ok([stagedRow, appliedRow])
    );
    render(<LearningLogPanel targetId='t1' onProfileChanged={jest.fn()} />);

    expect(
      await screen.findByText(/Staged for review \(1\)/i)
    ).toBeInTheDocument();
    // Diff chips from the staged row.
    expect(screen.getByText('recruiting agency')).toBeInTheDocument();
    expect(screen.getByText('rust ×2')).toBeInTheDocument();
    expect(screen.getByText(/42% confident/i)).toBeInTheDocument();
    expect(
      screen.getByText(/would move 4\/20 recent jobs/i)
    ).toBeInTheDocument();
    // History section + the applied row.
    expect(screen.getByText('History')).toBeInTheDocument();
    expect(screen.getByText('Applied')).toBeInTheDocument();
    expect(screen.getByText('golang ×3')).toBeInTheDocument();
    // Staged-row actions.
    expect(
      screen.getByRole('button', { name: /^Apply$/i })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /^Reject$/i })
    ).toBeInTheDocument();
  });

  it('applies a staged patch: POSTs to .../apply, toasts success, refreshes the profile', async () => {
    const onProfileChanged = jest.fn();
    (global.fetch as jest.Mock)
      .mockResolvedValueOnce(ok([stagedRow]))
      .mockResolvedValueOnce(
        ok({
          log: { ...stagedRow, status: 'applied' },
          applied: true,
          profile_version_after: 4,
        })
      )
      .mockResolvedValueOnce(ok([{ ...stagedRow, status: 'applied' }]));
    const user = userEvent.setup();
    render(
      <LearningLogPanel targetId='t1' onProfileChanged={onProfileChanged} />
    );

    await user.click(await screen.findByRole('button', { name: /^Apply$/i }));

    await waitFor(() =>
      expect(global.fetch).toHaveBeenCalledWith(
        '/api/targets/t1/learn/log-staged/apply',
        expect.objectContaining({ method: 'POST' })
      )
    );
    await waitFor(() => expect(onProfileChanged).toHaveBeenCalled());
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({ variant: 'success' })
    );
  });

  it('rejects a staged patch: POSTs to .../reject and does NOT refresh the profile', async () => {
    const onProfileChanged = jest.fn();
    (global.fetch as jest.Mock)
      .mockResolvedValueOnce(ok([stagedRow]))
      .mockResolvedValueOnce(
        ok({
          log: { ...stagedRow, status: 'rejected' },
          applied: false,
          profile_version_after: null,
        })
      )
      .mockResolvedValueOnce(ok([{ ...stagedRow, status: 'rejected' }]));
    const user = userEvent.setup();
    render(
      <LearningLogPanel targetId='t1' onProfileChanged={onProfileChanged} />
    );

    await user.click(await screen.findByRole('button', { name: /^Reject$/i }));

    await waitFor(() =>
      expect(global.fetch).toHaveBeenCalledWith(
        '/api/targets/t1/learn/log-staged/reject',
        expect.objectContaining({ method: 'POST' })
      )
    );
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({ variant: 'success' })
    );
    expect(onProfileChanged).not.toHaveBeenCalled();
  });

  it('toasts an error and does not refresh when apply fails', async () => {
    const onProfileChanged = jest.fn();
    (global.fetch as jest.Mock)
      .mockResolvedValueOnce(ok([stagedRow]))
      .mockResolvedValueOnce(fail(500));
    const user = userEvent.setup();
    render(
      <LearningLogPanel targetId='t1' onProfileChanged={onProfileChanged} />
    );

    await user.click(await screen.findByRole('button', { name: /^Apply$/i }));

    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: 'error' })
      )
    );
    expect(onProfileChanged).not.toHaveBeenCalled();
  });

  it('"Check for updates" with nothing to learn shows an info toast', async () => {
    (global.fetch as jest.Mock)
      .mockResolvedValueOnce(ok([]))
      .mockResolvedValueOnce(ok(null));
    const user = userEvent.setup();
    render(<LearningLogPanel targetId='t1' onProfileChanged={jest.fn()} />);

    await user.click(
      await screen.findByRole('button', { name: /check for updates/i })
    );

    await waitFor(() =>
      expect(global.fetch).toHaveBeenCalledWith(
        '/api/targets/t1/learn-llm',
        expect.objectContaining({ method: 'POST' })
      )
    );
    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          variant: 'info',
          title: expect.stringMatching(/No new patterns/i),
        })
      )
    );
  });

  it('"Check for updates" that stages a patch shows a success toast and refetches', async () => {
    const onProfileChanged = jest.fn();
    (global.fetch as jest.Mock)
      .mockResolvedValueOnce(ok([]))
      .mockResolvedValueOnce(
        ok({ log: stagedRow, applied: false, profile_version_after: null })
      )
      .mockResolvedValueOnce(ok([stagedRow]));
    const user = userEvent.setup();
    render(
      <LearningLogPanel targetId='t1' onProfileChanged={onProfileChanged} />
    );

    await user.click(
      await screen.findByRole('button', { name: /check for updates/i })
    );

    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          variant: 'success',
          title: expect.stringMatching(/staged for your review/i),
        })
      )
    );
    // applied:false -> the profile didn't change, so no parent refresh.
    expect(onProfileChanged).not.toHaveBeenCalled();
  });
});
