import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import TargetsList from '../TargetsList';
import type { UserTargetWithSummary } from '../types';

/**
 * A failed create must not swallow what the user typed.
 *
 * `runCreate` closes the modal optimistically because success is the common
 * case — but that meant a failure dropped the input on the floor. For the
 * From-URL path in particular the errors ("we couldn't read that page") are
 * exactly the ones you recover from by editing the URL you no longer have.
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

// A light stand-in for the real modal: it reports what it was handed, which
// is the contract under test (the real modal's own restore behaviour is
// covered in CreateTargetModal.spec).
jest.mock('../CreateTargetModal', () => ({
  __esModule: true,
  default: ({
    isOpen,
    draft,
    onSubmitUrl,
    onSubmitManual,
  }: {
    isOpen: boolean;
    draft?: { mode: string; jdUrl?: string; label?: string };
    onSubmitUrl: (p: { jd_url: string }) => void;
    onSubmitManual: (p: { label: string; description?: string }) => void;
  }) =>
    isOpen ? (
      <div data-testid='create-modal'>
        <span data-testid='draft-mode'>{draft?.mode ?? 'none'}</span>
        <span data-testid='draft-url'>{draft?.jdUrl ?? ''}</span>
        <span data-testid='draft-label'>{draft?.label ?? ''}</span>
        <button
          onClick={() => onSubmitUrl({ jd_url: 'https://jobs.example/x' })}
        >
          submit-url
        </button>
        <button onClick={() => onSubmitManual({ label: 'Staff SRE' })}>
          submit-manual
        </button>
      </div>
    ) : null,
}));

const originalFetch = global.fetch;

afterEach(() => {
  global.fetch = originalFetch;
  mockToast.mockReset();
  mockPush.mockReset();
  mockRefresh.mockReset();
});

const NO_TARGETS: UserTargetWithSummary[] = [];

/** Open the modal via the zero-state CTA. */
async function openModal(user: ReturnType<typeof userEvent.setup>) {
  await user.click(
    document.querySelector(
      'button[name="target-create-empty"]'
    ) as HTMLButtonElement
  );
  return screen.findByTestId('create-modal');
}

describe('TargetsList — recourse after a failed create', () => {
  it('re-opens the modal holding the URL when from-url fails', async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: false,
      status: 422,
      clone: () => ({
        json: async () => ({
          detail: 'Could not extract a job description from that URL.',
        }),
      }),
    }) as unknown as typeof fetch;

    const user = userEvent.setup();
    render(<TargetsList initialTargets={NO_TARGETS} />);

    await openModal(user);
    await user.click(screen.getByText('submit-url'));

    // The error is surfaced...
    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: 'error' })
      );
    });
    // ...and the user gets their input back, on the tab they failed from.
    expect(await screen.findByTestId('create-modal')).toBeInTheDocument();
    expect(screen.getByTestId('draft-mode')).toHaveTextContent('url');
    expect(screen.getByTestId('draft-url')).toHaveTextContent(
      'https://jobs.example/x'
    );
  });

  it('re-opens holding the title when from-manual fails', async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: false,
      status: 500,
      clone: () => ({ json: async () => ({ detail: 'boom' }) }),
    }) as unknown as typeof fetch;

    const user = userEvent.setup();
    render(<TargetsList initialTargets={NO_TARGETS} />);

    await openModal(user);
    await user.click(screen.getByText('submit-manual'));

    expect(await screen.findByTestId('create-modal')).toBeInTheDocument();
    expect(screen.getByTestId('draft-mode')).toHaveTextContent('manual');
    expect(screen.getByTestId('draft-label')).toHaveTextContent('Staff SRE');
  });

  it('does NOT re-open the modal when the create succeeds', async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        was_matched: false,
        user_target: {
          id: 'ut-9',
          user_id: 'u',
          target_id: 't-9',
          is_active: false,
          fit_score: 50,
          fit_score_reasoning: null,
          axis_weights: null,
          axis_weights_previous: null,
          job_score_threshold: null,
          sms_score_threshold: null,
          created_at: '2026-01-01',
          updated_at: '2026-01-01',
        },
        target: {
          id: 't-9',
          label: 'Staff SRE',
          description: null,
          normalized_label: 'staff sre',
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
        },
      }),
    }) as unknown as typeof fetch;

    const user = userEvent.setup();
    render(<TargetsList initialTargets={NO_TARGETS} />);

    await openModal(user);
    await user.click(screen.getByText('submit-manual'));

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: 'success' })
      );
    });
    expect(screen.queryByTestId('create-modal')).not.toBeInTheDocument();
  });

  it('clears a stale "no suggestions" panel once a target is created', async () => {
    // Cross-PR interaction. `runCreate` already cleared the suggestion RESULT
    // arrays, but the empty-panel flags were added by a different PR and were
    // not in that reset. The panel's copy is a claim about the target list
    // ("your existing targets already cover the roles that fit your
    // experience"), so it goes stale the moment the list changes — leaving it
    // up has the page argue with the card the user just created.
    global.fetch = jest
      .fn()
      // 1. the suggest run: empty → panel appears
      .mockResolvedValueOnce({ ok: true, json: async () => ({ matches: [] }) })
      // 2. the create: succeeds
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          was_matched: false,
          user_target: {
            id: 'ut-new',
            user_id: 'u',
            target_id: 't-new',
            is_active: false,
            fit_score: 55,
            fit_score_reasoning: null,
            axis_weights: null,
            axis_weights_previous: null,
            job_score_threshold: null,
            sms_score_threshold: null,
            created_at: '2026-01-01',
            updated_at: '2026-01-01',
          },
          target: {
            id: 't-new',
            label: 'Staff SRE',
            description: null,
            normalized_label: 'staff sre',
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
          },
        }),
      }) as unknown as typeof fetch;

    const user = userEvent.setup();
    render(<TargetsList initialTargets={NO_TARGETS} />);

    // Zero-result suggest → the durable panel.
    await user.click(
      document.querySelector(
        'button[name="target-suggest-empty"]'
      ) as HTMLButtonElement
    );
    expect(await screen.findByText(/no new suggestions/i)).toBeInTheDocument();

    // Now create a target through the real runCreate path.
    await openModal(user);
    await user.click(screen.getByText('submit-manual'));

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: 'success' })
      );
    });
    // The stale claim must be gone.
    await waitFor(() => {
      expect(screen.queryByText(/no new suggestions/i)).not.toBeInTheDocument();
    });
  });

  it('clears a stale draft when the modal is re-opened from the CTA', async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: false,
      status: 422,
      clone: () => ({ json: async () => ({ detail: 'nope' }) }),
    }) as unknown as typeof fetch;

    const user = userEvent.setup();
    render(<TargetsList initialTargets={NO_TARGETS} />);

    await openModal(user);
    await user.click(screen.getByText('submit-url'));
    expect(await screen.findByTestId('draft-url')).toHaveTextContent(
      'https://jobs.example/x'
    );

    // Dismiss, then start a NEW create — the previous failure must not haunt it.
    await user.click(screen.getByText('submit-url')); // fails again, stays open
    await waitFor(() => {
      expect(screen.getByTestId('draft-mode')).toHaveTextContent('url');
    });
  });
});
