import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import TargetDetail from '../TargetDetail';
import type { JobTarget, UserTarget } from '../../types';

/**
 * The detail header must report membership state in BOTH directions, and let
 * the user act on what it says.
 *
 * Previously only the active branch rendered a badge, so "inactive" was
 * signalled by the ABSENCE of one — indistinguishable from "still loading",
 * and invisible to anyone without an active target to compare against. And
 * activation lived only in the /targets card kebab, so arriving here by deep
 * link left no way to change what the badge reported. A user could tune weight
 * axes and preferences at length on a target that was matching nothing.
 */

const mockToast = jest.fn();
jest.mock('@/state/Toast/ToastProvider', () => ({
  useToast: () => ({ toast: mockToast }),
}));

jest.mock('next/navigation', () => ({
  useRouter: () => ({
    push: jest.fn(),
    prefetch: jest.fn(),
    replace: jest.fn(),
  }),
  usePathname: () => '/targets/t-1',
  useSearchParams: () => new URLSearchParams(),
}));

jest.mock('../ScoringProfileView', () => ({
  __esModule: true,
  default: () => <div data-testid='scoring-profile-view-stub' />,
}));
jest.mock('../ReferenceJDList', () => ({
  __esModule: true,
  default: () => <div data-testid='reference-jd-list-stub' />,
}));
jest.mock('../TargetDetailSkeleton', () => ({
  __esModule: true,
  default: () => <div data-testid='target-detail-skeleton-stub' />,
}));
jest.mock('../AxisWeightsEditor', () => ({
  __esModule: true,
  default: () => <div data-testid='axis-weights-editor-stub' />,
}));
jest.mock('../NotificationThresholdsEditor', () => ({
  __esModule: true,
  default: () => <div data-testid='notification-thresholds-stub' />,
}));
jest.mock('../TargetPreferencesEditor', () => ({
  __esModule: true,
  default: () => <div data-testid='target-preferences-stub' />,
}));
jest.mock('../LearningLogPanel', () => ({
  __esModule: true,
  default: () => <div data-testid='learning-log-stub' />,
}));

const ORIGINAL_FETCH = global.fetch;

const TARGET: JobTarget = {
  id: 't-1',
  label: 'Senior Frontend Engineer',
  description: null,
  normalized_label: null,
  scoring_profile: {
    categories: {},
    seniority: { level: null, signals: [] },
    domain: { signals: [], weight: 0.5 },
    negative: { keywords: [], weight: -10 },
  },
  search_keywords: [],
  activation_status: 'ready',
  profile_version: 1,
  app_active: true,
  created_at: '2026-01-01',
  updated_at: '2026-01-01',
};

function userTarget(isActive: boolean): UserTarget {
  return {
    id: 'ut-1',
    user_id: 'u-1',
    target_id: 't-1',
    is_active: isActive,
    fit_score: 71,
    fit_score_reasoning: null,
    axis_weights: null,
    axis_weights_previous: null,
    job_score_threshold: null,
    sms_score_threshold: null,
    created_at: '2026-01-01',
    updated_at: '2026-01-01',
  };
}

/** Serve the detail-page GETs, with the membership row under test. */
function mockFetch(isActive: boolean, overrides: Record<string, unknown> = {}) {
  return jest.fn().mockImplementation((input: string, init?: RequestInit) => {
    if (init?.method === 'POST') {
      return Promise.resolve(
        overrides['post'] ?? { ok: true, json: async () => TARGET }
      );
    }
    if (input.endsWith('/user-target')) {
      return Promise.resolve({
        ok: true,
        json: async () => ({
          user_target: userTarget(isActive),
          target: TARGET,
        }),
      });
    }
    if (input.endsWith('/reference-jds')) {
      return Promise.resolve({
        ok: true,
        json: async () => ({ reference_jds: [] }),
      });
    }
    return Promise.resolve({ ok: true, json: async () => TARGET });
  });
}

beforeEach(() => {
  jest.clearAllMocks();
});

afterAll(() => {
  global.fetch = ORIGINAL_FETCH;
});

describe('TargetDetail — membership state in the header', () => {
  it('renders an Inactive badge for a paused membership', async () => {
    global.fetch = mockFetch(false) as unknown as typeof fetch;

    render(<TargetDetail id='t-1' />);

    expect(await screen.findByText('Inactive')).toBeInTheDocument();
    expect(screen.queryByText('Active')).not.toBeInTheDocument();
  });

  it('still renders the Active badge for an active membership', async () => {
    global.fetch = mockFetch(true) as unknown as typeof fetch;

    render(<TargetDetail id='t-1' />);

    expect(await screen.findByText('Active')).toBeInTheDocument();
    expect(screen.queryByText('Inactive')).not.toBeInTheDocument();
  });

  it('offers Activate on a paused target and POSTs /activate', async () => {
    const fetchMock = mockFetch(false);
    global.fetch = fetchMock as unknown as typeof fetch;

    const user = userEvent.setup();
    render(<TargetDetail id='t-1' />);

    const btn = await screen.findByRole('button', { name: /^activate$/i });
    await user.click(btn);

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/targets/t-1/activate',
        expect.objectContaining({ method: 'POST' })
      );
    });
    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: 'success' })
      );
    });
  });

  it('offers Deactivate on an active target and POSTs /deactivate', async () => {
    const fetchMock = mockFetch(true);
    global.fetch = fetchMock as unknown as typeof fetch;

    const user = userEvent.setup();
    render(<TargetDetail id='t-1' />);

    const btn = await screen.findByRole('button', { name: /^deactivate$/i });
    await user.click(btn);

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/targets/t-1/deactivate',
        expect.objectContaining({ method: 'POST' })
      );
    });
  });

  it('surfaces a failed activation instead of showing a state the server refused', async () => {
    const fetchMock = mockFetch(false, {
      post: {
        ok: false,
        status: 409,
        clone: () => ({
          json: async () => ({ detail: 'You already have 1 active target.' }),
        }),
      },
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    const user = userEvent.setup();
    render(<TargetDetail id='t-1' />);

    await user.click(
      await screen.findByRole('button', { name: /^activate$/i })
    );

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: 'error' })
      );
    });
    // The badge must NOT have flipped — no optimistic lie about server state.
    expect(screen.getByText('Inactive')).toBeInTheDocument();
    expect(screen.queryByText('Active')).not.toBeInTheDocument();
  });

  it('renders neither badge nor toggle when the membership could not be read', async () => {
    // `fetchUserTarget` swallows its error by design (the page still works
    // without the membership row), so a failed /user-target renders the header
    // with `userTarget === null`. That state must NOT read as "Inactive" —
    // the whole point of adding the inactive badge is that absence stops
    // carrying meaning, so absence must now mean only "unknown".
    global.fetch = jest.fn().mockImplementation((input: string) => {
      if (input.endsWith('/user-target'))
        return Promise.resolve({ ok: false, status: 500 });
      if (input.endsWith('/reference-jds'))
        return Promise.resolve({
          ok: true,
          json: async () => ({ reference_jds: [] }),
        });
      return Promise.resolve({ ok: true, json: async () => TARGET });
    }) as unknown as typeof fetch;

    render(<TargetDetail id='t-1' />);

    // Header rendered (heading present) but membership unknown.
    expect(
      await screen.findByRole('heading', {
        level: 1,
        name: 'Senior Frontend Engineer',
      })
    ).toBeInTheDocument();
    expect(screen.queryByText('Inactive')).not.toBeInTheDocument();
    expect(screen.queryByText('Active')).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /^activate$/i })
    ).not.toBeInTheDocument();
  });
});
