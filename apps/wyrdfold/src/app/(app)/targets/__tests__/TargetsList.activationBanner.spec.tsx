import React from 'react';
import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';
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
 * `/targets` used to be the one page that never mentioned activation.
 *
 * Found in the second /targets sweep (2026-08-14) on an account whose ten
 * targets were ALL inactive, so nothing was being matched at all. Home said
 * "Activate a target so we can match incoming jobs" → Manage targets. /jobs
 * said "No active targets. Activate a target to start seeing matched jobs" →
 * Go to Targets. Both correctly diagnosed the problem and handed the user to
 * this page — which then said nothing: a grid of cards each carrying a small
 * grey "Inactive" chip, with the word "Activate" appearing nowhere, because it
 * lives one level down inside a per-card menu.
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
  mockRefresh.mockReset();
  mockPush.mockReset();
});

/** Fully-derived entry so the deriving-poll loop stays idle. */
function makeEntry(
  id: string,
  label: string,
  isActive: boolean
): UserTargetWithSummary {
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
    is_active: isActive,
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

const banner = () => screen.queryByText(/nothing is being matched/i);

describe('TargetsList — "no targets are active" banner', () => {
  it('warns when every target is inactive', () => {
    render(
      <TargetsList
        initialTargets={[
          makeEntry('t-1', 'Senior Frontend Engineer', false),
          makeEntry('t-2', 'Staff Full-Stack Engineer', false),
        ]}
      />
    );
    expect(banner()).toBeInTheDocument();
    // ...and names where the control actually lives, which is the whole point:
    // the recourse existed, it was just undiscoverable.
    expect(screen.getByText(/menu on whichever target/i)).toBeInTheDocument();
  });

  it('stays hidden as soon as one target is active', () => {
    render(
      <TargetsList
        initialTargets={[
          makeEntry('t-1', 'Senior Frontend Engineer', true),
          makeEntry('t-2', 'Staff Full-Stack Engineer', false),
        ]}
      />
    );
    expect(banner()).not.toBeInTheDocument();
  });

  it('stays hidden with no targets, where the empty state already speaks', () => {
    render(<TargetsList initialTargets={[]} />);
    expect(banner()).not.toBeInTheDocument();
    expect(screen.getByText(/No targets yet/i)).toBeInTheDocument();
  });

  it('disappears when the user activates a target', async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      json: async () => ({}),
    }) as unknown as typeof fetch;

    const user = userEvent.setup();
    render(
      <TargetsList
        initialTargets={[makeEntry('t-1', 'Senior Frontend Engineer', false)]}
      />
    );
    expect(banner()).toBeInTheDocument();

    await user.click(
      screen.getByRole('button', {
        name: /actions for senior frontend engineer/i,
      })
    );
    await user.click(screen.getByRole('menuitem', { name: /^activate$/i }));

    expect(banner()).not.toBeInTheDocument();
  });
});

describe('TargetCard actions menu', () => {
  /**
   * The trigger is an icon with `aria-hidden`, so the a11y tree reported
   * `button "(unnamed)"` — ten of them on a full page, with no way to tell
   * which card a menu belonged to.
   */
  it('names its trigger after the target', () => {
    render(
      <TargetsList
        initialTargets={[
          makeEntry('t-1', 'Senior Frontend Engineer', false),
          makeEntry('t-2', 'Staff Full-Stack Engineer', false),
        ]}
      />
    );
    expect(
      screen.getByRole('button', {
        name: /actions for senior frontend engineer/i,
      })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', {
        name: /actions for staff full-stack engineer/i,
      })
    ).toBeInTheDocument();
  });
});

describe('TargetsList — suggestion panels are dismissible', () => {
  /**
   * A 20-50s LLM result used to sit on the page until a full reload: there was
   * no close control on either panel.
   */
  it('dismisses the experience-suggestions panel', async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        matches: [
          {
            suggestion: {
              label: 'Platform Engineer',
              description: 'Platform roles.',
              core_skills: ['Kubernetes'],
            },
            matched_target: null,
            is_new: true,
          },
        ],
      }),
    }) as unknown as typeof fetch;

    const user = userEvent.setup();
    render(
      <TargetsList
        initialTargets={[makeEntry('t-1', 'Senior Frontend Engineer', true)]}
      />
    );

    await user.click(
      screen.getByRole('button', { name: /suggest from experience/i })
    );
    expect(await screen.findByText('Platform Engineer')).toBeInTheDocument();

    await user.click(
      document.querySelector(
        'button[name="target-suggest-dismiss"]'
      ) as HTMLElement
    );

    expect(screen.queryByText('Platform Engineer')).not.toBeInTheDocument();
    expect(
      screen.queryByText(/suggested targets from your experience/i)
    ).not.toBeInTheDocument();
  });

  it('dismisses the lateral-roles panel', async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        suggestions: [
          {
            label: 'Staff Backend Engineer – Platform Security',
            confidence: 88,
            primary_industry: 'Fintech',
            one_line_reasoning: 'Security hardening is direct evidence.',
            lateral_relationship: 'Same staff altitude, different niche.',
          },
        ],
      }),
    }) as unknown as typeof fetch;

    const user = userEvent.setup();
    render(
      <TargetsList
        initialTargets={[makeEntry('t-1', 'Senior Frontend Engineer', true)]}
      />
    );

    await user.click(
      screen.getByRole('button', { name: /suggest lateral roles/i })
    );
    expect(
      await screen.findByText('Staff Backend Engineer – Platform Security')
    ).toBeInTheDocument();

    await user.click(
      document.querySelector(
        'button[name="target-suggest-lateral-dismiss"]'
      ) as HTMLElement
    );

    expect(
      screen.queryByText('Staff Backend Engineer – Platform Security')
    ).not.toBeInTheDocument();
  });
});
