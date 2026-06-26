import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import TargetsList from '../TargetsList';
import {
  emptyScoringProfile,
  toSummary,
  type JobTarget,
  type LateralSuggestion,
  type UserTarget,
  type UserTargetWithSummary,
} from '../types';

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

// CreateTargetModal renders a Dialog/Portal; stub it out so the list test
// stays focused on the list surface (modal mechanics covered separately).
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

/** A fully-derived (ready, fit-scored) entry so the deriving-poll loop stays
 * idle and the only fetch calls are the ones the lateral flow issues. */
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
    is_active: true,
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

function makeLateral(over: Partial<LateralSuggestion> = {}): LateralSuggestion {
  return {
    label: 'Director of Customer Success Operations',
    one_line_reasoning: '5 years building CX-Ops; maps 1:1 onto CS Ops.',
    confidence: 92,
    lateral_relationship: 'Same altitude, different industry vocabulary.',
    primary_industry: 'B2B SaaS',
    seniority_hint: 'director',
    ...over,
  };
}

function renderPopulated() {
  return render(
    <TargetsList
      initialTargets={[makeReadyEntry('t-1', 'CX Operations Lead')]}
    />
  );
}

describe('TargetsList — lateral suggestions', () => {
  it('POSTs to /api/targets/suggest-lateral and renders the returned roles', async () => {
    const fetchMock = jest.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        suggestions: [
          makeLateral(),
          makeLateral({
            label: 'VP of Revenue Operations',
            confidence: 64,
            primary_industry: null,
            seniority_hint: 'vp',
            lateral_relationship: 'One rung up — stretch.',
          }),
        ],
      }),
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    const user = userEvent.setup();
    renderPopulated();

    await user.click(
      screen.getByRole('button', { name: /suggest lateral roles/i })
    );

    // The trigger hits the lateral BFF route (not the onboarding /suggest one).
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/targets/suggest-lateral',
        expect.objectContaining({ method: 'POST' })
      );
    });

    // Both suggestions render, with confidence + relationship copy.
    expect(
      await screen.findByText('Director of Customer Success Operations')
    ).toBeInTheDocument();
    expect(screen.getByText('VP of Revenue Operations')).toBeInTheDocument();
    expect(screen.getByText('92% match')).toBeInTheDocument();
    expect(
      screen.getByText(/same altitude, different industry vocabulary/i)
    ).toBeInTheDocument();
  });

  it('shows a loading spinner on the trigger while the request is in flight', async () => {
    // Never resolves — exercises the loading state.
    const fetchMock = jest.fn().mockReturnValue(
      new Promise(() => {
        // intentionally empty
      })
    );
    global.fetch = fetchMock as unknown as typeof fetch;

    const user = userEvent.setup();
    renderPopulated();

    await user.click(
      screen.getByRole('button', { name: /suggest lateral roles/i })
    );

    expect(
      await screen.findByLabelText(/suggesting lateral roles/i)
    ).toBeInTheDocument();
  });

  it('toasts an info message when no lateral roles come back', async () => {
    const fetchMock = jest.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ suggestions: [] }),
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    const user = userEvent.setup();
    renderPopulated();

    await user.click(
      screen.getByRole('button', { name: /suggest lateral roles/i })
    );

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          variant: 'info',
          title: expect.stringMatching(/no lateral roles/i),
        })
      );
    });
  });

  it('toasts an error (and renders no cards) when the lateral request fails', async () => {
    // The error body is a well-formed payload WITH a json() method and real
    // suggestions, so this only goes to the error toast because the handler
    // honours `!res.ok` — not because json() happened to throw. If the
    // status guard were dropped, these suggestions would render and the
    // error toast would never fire (this is the regression this test pins).
    const errorBody = { suggestions: [makeLateral()] };
    const fetchMock = jest.fn().mockResolvedValue({
      ok: false,
      status: 500,
      // extractApiError reads the body via res.clone().json(); the handler
      // itself reads res.json() on the success path. Provide both.
      json: async () => errorBody,
      clone: () => ({ json: async () => ({ detail: 'boom' }) }),
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    const user = userEvent.setup();
    renderPopulated();

    await user.click(
      screen.getByRole('button', { name: /suggest lateral roles/i })
    );

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: 'error' })
      );
    });
    // The failed payload's suggestions must NOT have leaked into the grid.
    expect(
      screen.queryByText('Director of Customer Success Operations')
    ).toBeNull();
  });

  it('creates a target from a lateral suggestion via /from-manual', async () => {
    const created = makeReadyEntry(
      't-new',
      'Director of Customer Success Operations'
    );
    const fetchMock = jest.fn((url: string, _init?: RequestInit) => {
      if (url === '/api/targets/suggest-lateral') {
        return Promise.resolve({
          ok: true,
          json: async () => ({ suggestions: [makeLateral()] }),
        });
      }
      // /from-manual returns a CreateOrLinkResult.
      return Promise.resolve({
        ok: true,
        json: async () => ({
          user_target: created.user_target,
          target: makeReadyEntry(
            't-new',
            'Director of Customer Success Operations'
          ).target,
          was_matched: false,
        }),
      });
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    const user = userEvent.setup();
    renderPopulated();

    await user.click(
      screen.getByRole('button', { name: /suggest lateral roles/i })
    );

    // Multiple "Add Target" buttons share the same accessible text (the
    // visible label), so disambiguate via the per-suggestion HTML `name`
    // the component sets (add-lateral-<label>).
    await screen.findByText('Director of Customer Success Operations');
    const addBtn = document.querySelector(
      'button[name="add-lateral-Director of Customer Success Operations"]'
    ) as HTMLButtonElement;
    expect(addBtn).toBeInTheDocument();
    await user.click(addBtn);

    // The lateral label (with its reasoning as the description) is POSTed to
    // the create-or-link endpoint.
    await waitFor(() => {
      const manualCall = fetchMock.mock.calls.find(
        ([u]) => u === '/api/targets/from-manual'
      );
      if (!manualCall) throw new Error('expected a /from-manual POST');
      const body = JSON.parse(
        (manualCall[1] as RequestInit).body as string
      ) as { label: string; description: string };
      expect(body.label).toBe('Director of Customer Success Operations');
      expect(body.description).toMatch(/CX-Ops/);
    });

    // Success toast fires and the suggestion is consumed from the grid.
    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: 'success' })
      );
    });
  });
});
