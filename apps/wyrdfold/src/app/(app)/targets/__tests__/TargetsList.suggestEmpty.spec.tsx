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
 * A zero-result run of either suggest action must leave a DURABLE trace.
 *
 * Both actions are LLM-backed and routinely run 20-50s, while the info toast
 * that reports "nothing found" auto-dismisses after 4s (ToastProvider). A user
 * who looks away for the duration — i.e. the normal case for a 30s wait — used
 * to come back to a page byte-identical to the one they left, with no way to
 * tell whether the click had registered, failed, or simply found nothing.
 *
 * The toast is still asserted (it serves whoever is watching); these specs pin
 * the second layer, matching LearningLogPanel's transient+durable treatment.
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
  jest.useRealTimers();
  global.fetch = originalFetch;
  mockToast.mockReset();
  mockRefresh.mockReset();
  mockPush.mockReset();
});

/** Fully-derived entry so the deriving-poll loop stays idle. */
function makeReadyEntry(id: string, label: string): UserTargetWithSummary {
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
    is_active: true,
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

function renderPopulated() {
  return render(
    <TargetsList
      initialTargets={[makeReadyEntry('t-1', 'CX Operations Lead')]}
    />
  );
}

describe('TargetsList — durable empty state after a zero-result suggest run', () => {
  it('renders a persistent "No new suggestions" panel when /suggest returns []', async () => {
    const fetchMock = jest.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ matches: [] }),
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    const user = userEvent.setup();
    renderPopulated();

    // Precondition: nothing on the page says this before the run. Without it
    // the assertion below could pass on static copy and prove nothing.
    expect(screen.queryByText(/no new suggestions/i)).not.toBeInTheDocument();

    await user.click(
      screen.getByRole('button', { name: /suggest from experience/i })
    );

    // The durable panel — still in the DOM, unlike the 4s toast.
    expect(await screen.findByText(/no new suggestions/i)).toBeInTheDocument();
    expect(
      screen.getByText(/already cover the roles that fit your experience/i)
    ).toBeInTheDocument();

    // And it offers a way forward rather than being a dead end.
    expect(
      document.querySelector('button[name="target-suggest-retry"]')
    ).toBeInTheDocument();
  });

  it('renders a persistent panel when /suggest-lateral returns []', async () => {
    const fetchMock = jest.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ suggestions: [] }),
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    const user = userEvent.setup();
    renderPopulated();

    expect(
      screen.queryByText(/no lateral roles found/i)
    ).not.toBeInTheDocument();

    await user.click(
      screen.getByRole('button', { name: /suggest lateral roles/i })
    );

    expect(
      await screen.findByText(/no lateral roles found/i)
    ).toBeInTheDocument();
    expect(
      document.querySelector('button[name="target-suggest-lateral-retry"]')
    ).toBeInTheDocument();
  });

  it('does NOT render the empty panel when suggestions come back', async () => {
    const fetchMock = jest.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        matches: [
          {
            suggestion: {
              label: 'Staff Platform Engineer',
              description: 'Infra ownership at scale.',
              core_skills: ['Kubernetes'],
            },
            matched_target: null,
            is_new: true,
          },
        ],
      }),
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    const user = userEvent.setup();
    renderPopulated();

    await user.click(
      screen.getByRole('button', { name: /suggest from experience/i })
    );

    expect(
      await screen.findByText('Staff Platform Engineer')
    ).toBeInTheDocument();
    expect(screen.queryByText(/no new suggestions/i)).not.toBeInTheDocument();
  });

  it('clears a previous empty panel when a re-run returns results', async () => {
    const fetchMock = jest
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => ({ matches: [] }) })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          matches: [
            {
              suggestion: {
                label: 'Staff Platform Engineer',
                description: 'Infra ownership at scale.',
                core_skills: [],
              },
              matched_target: null,
              is_new: true,
            },
          ],
        }),
      });
    global.fetch = fetchMock as unknown as typeof fetch;

    const user = userEvent.setup();
    renderPopulated();

    await user.click(
      screen.getByRole('button', { name: /suggest from experience/i })
    );
    expect(await screen.findByText(/no new suggestions/i)).toBeInTheDocument();

    // Retry from inside the panel — the stale empty state must not persist
    // alongside fresh results.
    await user.click(
      document.querySelector(
        'button[name="target-suggest-retry"]'
      ) as HTMLButtonElement
    );

    expect(
      await screen.findByText('Staff Platform Engineer')
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByText(/no new suggestions/i)).not.toBeInTheDocument();
    });
  });

  it('keeps the info toast (the watching user still gets immediate feedback)', async () => {
    const fetchMock = jest.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ matches: [] }),
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    const user = userEvent.setup();
    renderPopulated();

    await user.click(
      screen.getByRole('button', { name: /suggest from experience/i })
    );

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          variant: 'info',
          title: expect.stringMatching(/no new suggestions/i),
        })
      );
    });
  });
});

describe('TargetsList — zero-state suggest button in-flight feedback', () => {
  it('swaps to a spinner while the request is in flight', async () => {
    // Never resolves — holds the in-flight state open.
    const fetchMock = jest.fn().mockReturnValue(
      new Promise(() => {
        // intentionally empty
      })
    );
    global.fetch = fetchMock as unknown as typeof fetch;

    const user = userEvent.setup();
    // No targets => the zero-state card renders `target-suggest-empty`.
    render(<TargetsList initialTargets={[]} />);

    const btn = document.querySelector(
      'button[name="target-suggest-empty"]'
    ) as HTMLButtonElement;
    expect(btn).toBeInTheDocument();
    expect(btn).not.toHaveAttribute('aria-busy', 'true');

    await user.click(btn);

    // Previously this button only went `disabled` — no spinner, no label
    // change — so a first-time user got the weakest signal of anyone during
    // the same 20-50s call.
    await waitFor(() => {
      expect(
        document.querySelector('button[name="target-suggest-empty"]')
      ).toHaveAttribute('aria-busy', 'true');
    });
    expect(await screen.findByLabelText(/suggesting/i)).toBeInTheDocument();
  });
});
