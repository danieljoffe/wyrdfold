import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import TargetDetail from '../TargetDetail';
import type { JobTarget } from '../../types';

const mockToast = jest.fn();
jest.mock('@/state/Toast/ToastProvider', () => ({
  useToast: () => ({ toast: mockToast }),
}));

// The kit Breadcrumbs uses useRouter for client-nav interception; the
// tab set reads/writes the URL (?tab=) via the router + search params.
jest.mock('next/navigation', () => ({
  useRouter: () => ({
    push: jest.fn(),
    prefetch: jest.fn(),
    replace: jest.fn(),
  }),
  usePathname: () => '/targets/t-1',
  useSearchParams: () => new URLSearchParams(),
}));

// Children are exercised in their own specs; stub them so this spec only
// proves the section composition + loading / not-found switch.
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

const USER_TARGET = {
  id: 'ut-1',
  user_id: 'u-1',
  target_id: 't-1',
  // The header badge reads the caller's OWN membership state (P0
  // re-semantics) — not the shared catalog flag.
  is_active: true,
  fit_score: null,
  fit_score_reasoning: null,
  axis_weights: null,
  axis_weights_previous: null,
  created_at: '2026-01-01',
  updated_at: '2026-01-01',
};

const TARGET: JobTarget = {
  id: 't-1',
  label: 'Senior Frontend Engineer',
  description: 'Frontend roles I want to pursue',
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

beforeEach(() => {
  jest.clearAllMocks();
});

afterAll(() => {
  global.fetch = ORIGINAL_FETCH;
});

describe('TargetDetail', () => {
  it('renders the skeleton while initial fetches are in flight', () => {
    global.fetch = jest
      .fn()
      .mockImplementation(
        () => new Promise(() => undefined)
      ) as unknown as typeof fetch;

    render(<TargetDetail id='t-1' />);
    expect(
      screen.getByTestId('target-detail-skeleton-stub')
    ).toBeInTheDocument();
  });

  it('renders the target heading and section stubs once loaded', async () => {
    global.fetch = jest.fn().mockImplementation((input: string) => {
      if (input.endsWith('/reference-jds')) {
        return Promise.resolve({
          ok: true,
          json: async () => ({ reference_jds: [] }),
        });
      }
      if (input.endsWith('/user-target')) {
        return Promise.resolve({
          ok: true,
          json: async () => ({ user_target: USER_TARGET, target: TARGET }),
        });
      }
      return Promise.resolve({ ok: true, json: async () => TARGET });
    }) as unknown as typeof fetch;

    render(<TargetDetail id='t-1' />);

    expect(
      await screen.findByRole('heading', {
        level: 1,
        name: /senior frontend engineer/i,
      })
    ).toBeInTheDocument();
    // The Scoring tab is the default: shared scoring model renders,
    // the other tabs' editors do NOT mount until selected (lazy per tab).
    expect(screen.getByTestId('scoring-profile-view-stub')).toBeInTheDocument();
    expect(screen.queryByTestId('reference-jd-list-stub')).toBeNull();
    expect(screen.queryByTestId('learning-log-stub')).toBeNull();
    // SEC-2 (#366): the shared label is view-only — no inline edit control.
    expect(screen.queryByRole('button', { name: /edit label/i })).toBeNull();
    // Tabs are present with the Fork B labels.
    for (const label of [
      'Scoring',
      'Preferences',
      'Reference JDs',
      'Learning',
    ]) {
      expect(screen.getByRole('tab', { name: label })).toBeInTheDocument();
    }
    // Fork C: the shared scoring model is visibly badged.
    expect(screen.getByText(/shared with co-searchers/i)).toBeInTheDocument();
    // Active badge
    expect(screen.getByText(/^active$/i)).toBeInTheDocument();
    // Breadcrumb trail (UX/IA §2.1) replaced the old "Back to targets" link.
    expect(
      screen.getByRole('navigation', { name: /breadcrumb/i })
    ).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Targets' })).toHaveAttribute(
      'href',
      '/targets'
    );
  });

  it('renders the "Target not found" state when the target fetch fails', async () => {
    global.fetch = jest.fn().mockImplementation((input: string) => {
      if (input.endsWith('/reference-jds')) {
        return Promise.resolve({
          ok: true,
          json: async () => ({ reference_jds: [] }),
        });
      }
      return Promise.resolve({ ok: false, json: async () => ({}) });
    }) as unknown as typeof fetch;

    render(<TargetDetail id='t-1' />);

    await waitFor(() => {
      expect(
        screen.getByRole('heading', { level: 1, name: /target not found/i })
      ).toBeInTheDocument();
    });
    expect(
      screen.getByRole('link', { name: /back to targets/i })
    ).toHaveAttribute('href', '/targets');
  });

  it('switching tabs lazily mounts that tab and fetches its data', async () => {
    const fetchCalls: string[] = [];
    global.fetch = jest.fn().mockImplementation((input: string) => {
      fetchCalls.push(input);
      if (input.endsWith('/reference-jds')) {
        return Promise.resolve({
          ok: true,
          json: async () => ({ reference_jds: [] }),
        });
      }
      return Promise.resolve({ ok: true, json: async () => TARGET });
    }) as unknown as typeof fetch;

    const user = userEvent.setup();
    render(<TargetDetail id='t-1' />);
    await screen.findByRole('heading', { level: 1 });

    // No JD fetch on mount — it belongs to its tab.
    expect(fetchCalls.some(u => u.endsWith('/reference-jds'))).toBe(false);

    await user.click(screen.getByRole('tab', { name: 'Reference JDs' }));

    await waitFor(() =>
      expect(screen.getByTestId('reference-jd-list-stub')).toBeInTheDocument()
    );
    expect(fetchCalls.some(u => u.endsWith('/reference-jds'))).toBe(true);
    // Default-tab content unmounted; Learning still never mounted.
    expect(screen.queryByTestId('scoring-profile-view-stub')).toBeNull();
    expect(screen.queryByTestId('learning-log-stub')).toBeNull();

    await user.click(screen.getByRole('tab', { name: 'Learning' }));
    await waitFor(() =>
      expect(screen.getByTestId('learning-log-stub')).toBeInTheDocument()
    );
  });
});
