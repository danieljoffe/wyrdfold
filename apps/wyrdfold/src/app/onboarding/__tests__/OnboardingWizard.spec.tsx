import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import OnboardingWizard from '../OnboardingWizard';

const mockPush = jest.fn();

jest.mock('next/navigation', () => ({
  useRouter: () => ({
    push: (...args: unknown[]) => mockPush(...args),
    replace: jest.fn(),
    refresh: jest.fn(),
    prefetch: jest.fn(),
    back: jest.fn(),
  }),
}));

// Stub heavy/network-bound child steps so the wizard's dispatch logic
// is the only thing under test. Each stub renders a single button labelled
// with its step name so role queries can drive navigation.
jest.mock('../ResumeUploader', () => ({
  __esModule: true,
  default: ({
    onComplete,
    onSkip,
  }: {
    onComplete: () => void;
    onSkip: () => void;
  }) => (
    <div data-testid='resume-uploader-stub'>
      <button type='button' onClick={onComplete}>
        resume-complete
      </button>
      <button type='button' onClick={onSkip}>
        resume-skip
      </button>
    </div>
  ),
}));

jest.mock('../JobUrlInput', () => ({
  __esModule: true,
  default: ({
    onComplete,
    onSkip,
  }: {
    onComplete: (data?: {
      postingId: string;
      title: string | null;
      descriptionHtml: string | null;
    }) => void;
    onSkip: () => void;
  }) => (
    <div data-testid='job-url-input-stub'>
      <button
        type='button'
        onClick={() =>
          onComplete({ postingId: 'p1', title: 'Eng', descriptionHtml: null })
        }
      >
        job-complete
      </button>
      <button type='button' onClick={onSkip}>
        job-skip
      </button>
    </div>
  ),
}));

jest.mock('../TargetSuggestions', () => ({
  __esModule: true,
  default: ({
    onComplete,
    onSkip,
  }: {
    onComplete: () => void;
    onSkip: () => void;
  }) => (
    <div data-testid='target-suggestions-stub'>
      <button type='button' onClick={onComplete}>
        targets-complete
      </button>
      <button type='button' onClick={onSkip}>
        targets-skip
      </button>
    </div>
  ),
}));

jest.mock('../IdentityStep', () => ({
  __esModule: true,
  default: ({
    onComplete,
    onSkip,
  }: {
    onComplete: () => void;
    onSkip: () => void;
  }) => (
    <div data-testid='identity-step-stub'>
      <button type='button' onClick={onComplete}>
        identity-complete
      </button>
      <button type='button' onClick={onSkip}>
        identity-skip
      </button>
    </div>
  ),
}));

jest.mock('../CompletionScreen', () => ({
  __esModule: true,
  default: () => <div data-testid='completion-screen-stub'>completion</div>,
}));

jest.mock('../../_components/ConversationChat', () => ({
  __esModule: true,
  default: ({
    onComplete,
    onSkip,
  }: {
    onComplete: () => void;
    onSkip: () => void;
  }) => (
    <div data-testid='conversation-chat-stub'>
      <button type='button' onClick={onComplete}>
        chat-complete
      </button>
      <button type='button' onClick={onSkip}>
        chat-skip
      </button>
    </div>
  ),
}));

const originalFetch = global.fetch;
const mockFetch = jest.fn();

beforeEach(() => {
  jest.clearAllMocks();
  mockFetch.mockResolvedValue({ ok: true, status: 200 });
  global.fetch = mockFetch as unknown as typeof fetch;
});

afterEach(() => {
  global.fetch = originalFetch;
});

describe('OnboardingWizard — resume mid-flow (#85)', () => {
  it('resumes at the persisted step when path + step are valid', () => {
    render(<OnboardingWizard initialPath='A' initialStep='upload-resume' />);
    // Path A at ``upload-resume`` → the ResumeUploader, not the chooser.
    expect(screen.getByTestId('resume-uploader-stub')).toBeInTheDocument();
    expect(
      screen.queryByText(/how would you like to get started\?/i)
    ).not.toBeInTheDocument();
  });

  it('falls back to the path chooser when the step is not part of the path', () => {
    // ``add-job`` exists only in Path A, not Path B → inconsistent → restart.
    render(<OnboardingWizard initialPath='B' initialStep='add-job' />);
    expect(
      screen.getByText(/how would you like to get started\?/i)
    ).toBeInTheDocument();
    expect(screen.queryByTestId('job-url-input-stub')).not.toBeInTheDocument();
  });

  it('falls back to the path chooser when no path is persisted', () => {
    render(<OnboardingWizard initialStep='upload-resume' />);
    expect(
      screen.getByText(/how would you like to get started\?/i)
    ).toBeInTheDocument();
  });

  it('does not fire a step PATCH just for resuming (no redundant mount write)', () => {
    render(<OnboardingWizard initialPath='A' initialStep='upload-resume' />);
    expect(mockFetch).not.toHaveBeenCalledWith(
      '/api/profile/onboarding/step',
      expect.anything()
    );
  });

  it('persists progress (path + step) on a step transition', async () => {
    const user = userEvent.setup();
    render(<OnboardingWizard />);
    // Picking Path A advances to ``identity`` → progress is persisted so a
    // later drop-out resumes there.
    await user.click(
      screen.getByRole('button', {
        name: /i have a resume and a role in mind/i,
      })
    );
    await waitFor(() =>
      expect(mockFetch).toHaveBeenCalledWith(
        '/api/profile/onboarding/step',
        expect.objectContaining({
          method: 'PATCH',
          body: JSON.stringify({ path: 'A', current_step: 'identity' }),
        })
      )
    );
  });
});

describe('OnboardingWizard — initial state', () => {
  it('renders the path chooser with the welcome heading', () => {
    render(<OnboardingWizard />);

    expect(
      screen.getByRole('heading', { level: 1, name: /welcome to wyrdfold/i })
    ).toBeInTheDocument();
    expect(
      screen.getByText(/how would you like to get started\?/i)
    ).toBeInTheDocument();
  });

  it('does not render a progress bar on the path chooser step', () => {
    render(<OnboardingWizard />);
    // ProgressBar renders role=progressbar — should be absent before path is picked
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
  });

  it('navigates to /dashboard when the user clicks Skip setup for now', async () => {
    const user = userEvent.setup();
    render(<OnboardingWizard />);

    await user.click(
      screen.getByRole('button', { name: /skip setup for now/i })
    );

    expect(mockPush).toHaveBeenCalledWith('/dashboard');
  });

  it('DEFERS (not completes) on Skip — the exit is recorded but resumable', async () => {
    // onboarding-sweep-2026-08-14 P1: completing on skip made "later"
    // permanent. The exit now posts /defer, which quiets the dashboard
    // gate while completed_at stays NULL so /onboarding still resumes.
    const user = userEvent.setup();
    render(<OnboardingWizard />);

    await user.click(
      screen.getByRole('button', { name: /skip setup for now/i })
    );

    expect(mockFetch).toHaveBeenCalledWith('/api/profile/onboarding/defer', {
      method: 'POST',
    });
    expect(mockFetch).not.toHaveBeenCalledWith(
      '/api/profile/onboarding/complete',
      expect.anything()
    );
  });

  // Regression for the "skip doesn't stick" bug: handleSkip used to fire
  // the flag POST un-awaited and navigate immediately, so the page
  // tore down before the request settled and the flag was never written
  // — the dashboard then re-fired onboarding on the next visit. The skip
  // MUST persist (await the POST) before navigation. Pre-fix, mockPush
  // was already called before the deferred fetch resolved → this fails.
  it('awaits the defer POST BEFORE navigating away (skip persists)', async () => {
    const user = userEvent.setup();

    // Deferred fetch we resolve by hand, to observe ordering: navigation
    // must NOT happen until the completion write has resolved.
    type FetchResult = { ok: boolean; status: number };
    const deferred: {
      promise: Promise<FetchResult>;
      resolve: (value: FetchResult) => void;
    } = (() => {
      let resolve!: (value: FetchResult) => void;
      const promise = new Promise<FetchResult>(res => {
        resolve = res;
      });
      return { promise, resolve };
    })();
    mockFetch.mockReturnValueOnce(deferred.promise);

    render(<OnboardingWizard />);

    await user.click(
      screen.getByRole('button', { name: /skip setup for now/i })
    );

    // POST is in flight but unresolved → we must still be on the wizard.
    expect(mockFetch).toHaveBeenCalledWith('/api/profile/onboarding/defer', {
      method: 'POST',
    });
    expect(mockPush).not.toHaveBeenCalled();

    // Once the defer write settles, navigation proceeds.
    deferred.resolve({ ok: true, status: 200 });
    await waitFor(() => expect(mockPush).toHaveBeenCalledWith('/dashboard'));
  });

  it('does NOT navigate and shows a retry when the defer POST fails on every attempt', async () => {
    // Persistent failure (network down on both the initial call and the
    // retry) → the flag never landed. Navigating would drop the user into
    // the dashboard's redirect loop, so we stay on the wizard and surface
    // a retry affordance instead.
    const user = userEvent.setup();
    mockFetch.mockRejectedValue(new Error('network down'));

    render(<OnboardingWizard />);

    await user.click(
      screen.getByRole('button', { name: /skip setup for now/i })
    );

    await waitFor(() =>
      expect(
        screen.getByText(/couldn.t save your progress/i)
      ).toBeInTheDocument()
    );
    expect(mockPush).not.toHaveBeenCalled();
    expect(
      screen.getByRole('button', { name: /try again/i })
    ).toBeInTheDocument();
  });

  it('navigates to /dashboard when a transient 5xx recovers on retry', async () => {
    const user = userEvent.setup();
    mockFetch
      .mockResolvedValueOnce({ ok: false, status: 503 })
      .mockResolvedValueOnce({ ok: true, status: 200 });

    render(<OnboardingWizard />);

    await user.click(
      screen.getByRole('button', { name: /skip setup for now/i })
    );

    await waitFor(() => expect(mockPush).toHaveBeenCalledWith('/dashboard'));
  });
});

describe('OnboardingWizard — skip semantics (2026-08-13 walkthrough)', () => {
  // The old wiring sent EVERY step's skip through the global exit, so
  // "skip the upload" silently completed the whole wizard — with the AI
  // down, the natural recovery click threw away the remaining steps.

  it('a step-level skip ADVANCES to the next step, not out of the wizard', async () => {
    const user = userEvent.setup();
    render(<OnboardingWizard />);

    await user.click(
      screen.getByRole('button', {
        name: /i have a resume and a role in mind/i,
      })
    );
    await user.click(screen.getByRole('button', { name: 'identity-skip' }));

    // Advanced to upload-resume; nothing completed, nowhere navigated.
    expect(screen.getByTestId('resume-uploader-stub')).toBeInTheDocument();
    expect(mockPush).not.toHaveBeenCalled();
    expect(mockFetch).not.toHaveBeenCalledWith(
      '/api/profile/onboarding/complete',
      expect.anything()
    );
  });

  it('skipping every step still walks the full path to completion', async () => {
    const user = userEvent.setup();
    render(<OnboardingWizard />);

    await user.click(
      screen.getByRole('button', {
        name: /i have a resume and a role in mind/i,
      })
    );
    await user.click(screen.getByRole('button', { name: 'identity-skip' }));
    await user.click(screen.getByRole('button', { name: 'resume-skip' }));
    await user.click(screen.getByRole('button', { name: 'job-skip' }));
    await user.click(screen.getByRole('button', { name: 'targets-skip' }));

    expect(screen.getByTestId('completion-screen-stub')).toBeInTheDocument();
    expect(mockPush).not.toHaveBeenCalled();
  });

  it('"Finish setup later" mid-path exits via the persisted-DEFER contract', async () => {
    // The exit records deferred_at (dashboard gate quiets down) but must
    // NEVER complete — completed_at=NULL is what keeps /onboarding
    // enterable so "later" actually exists (sweep 2026-08-14 P1).
    const user = userEvent.setup();
    render(<OnboardingWizard />);

    await user.click(
      screen.getByRole('button', {
        name: /i have a resume and a role in mind/i,
      })
    );
    await user.click(screen.getByRole('button', { name: 'identity-complete' }));

    await user.click(
      screen.getByRole('button', { name: /finish setup later/i })
    );

    await waitFor(() => expect(mockPush).toHaveBeenCalledWith('/dashboard'));
    expect(mockFetch).toHaveBeenCalledWith('/api/profile/onboarding/defer', {
      method: 'POST',
    });
    expect(mockFetch).not.toHaveBeenCalledWith(
      '/api/profile/onboarding/complete',
      expect.anything()
    );
  });

  it('offers no "Finish setup later" on the chooser or the completion step', async () => {
    const user = userEvent.setup();
    render(<OnboardingWizard />);

    // Chooser: the global exit is its own "Skip setup for now" button.
    expect(
      screen.queryByRole('button', { name: /finish setup later/i })
    ).not.toBeInTheDocument();

    await user.click(
      screen.getByRole('button', {
        name: /i have a resume and a role in mind/i,
      })
    );
    await user.click(screen.getByRole('button', { name: 'identity-skip' }));
    await user.click(screen.getByRole('button', { name: 'resume-skip' }));
    await user.click(screen.getByRole('button', { name: 'job-skip' }));
    await user.click(screen.getByRole('button', { name: 'targets-skip' }));

    // Completion: nothing left to skip.
    expect(
      screen.queryByRole('button', { name: /finish setup later/i })
    ).not.toBeInTheDocument();
  });
});

describe('OnboardingWizard — change path (sweep 2026-08-14 B1)', () => {
  it('offers no change-path link on the chooser itself', () => {
    render(<OnboardingWizard />);
    expect(
      screen.queryByRole('button', { name: /change path/i })
    ).not.toBeInTheDocument();
  });

  it('returns to the chooser and persists the reset so refresh does not resume the abandoned path', async () => {
    const user = userEvent.setup();
    render(<OnboardingWizard />);

    await user.click(
      screen.getByRole('button', {
        name: /i have a resume and a role in mind/i,
      })
    );
    expect(screen.getByTestId('identity-step-stub')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /change path/i }));

    // Back at the chooser…
    expect(
      screen.getByText(/how would you like to get started\?/i)
    ).toBeInTheDocument();
    // …and the persisted step is reset to 'path-chooser', which
    // resolveResume treats as a clean start — without this write a
    // mid-change refresh resumes INTO the abandoned path.
    await waitFor(() =>
      expect(mockFetch).toHaveBeenCalledWith(
        '/api/profile/onboarding/step',
        expect.objectContaining({
          method: 'PATCH',
          body: JSON.stringify({ current_step: 'path-chooser' }),
        })
      )
    );
  });

  it('a different path can be picked after changing course', async () => {
    const user = userEvent.setup();
    render(<OnboardingWizard />);

    await user.click(
      screen.getByRole('button', {
        name: /i have a resume and a role in mind/i,
      })
    );
    await user.click(screen.getByRole('button', { name: /change path/i }));
    await user.click(
      screen.getByRole('button', { name: /not sure where to start/i })
    );

    // Path C's first real step is identity, then conversation.
    expect(screen.getByTestId('identity-step-stub')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'identity-complete' }));
    expect(screen.getByTestId('conversation-chat-stub')).toBeInTheDocument();
  });
});

describe('OnboardingWizard — Path A (resume + role)', () => {
  it('dispatches to IdentityStep after selecting Path A', async () => {
    const user = userEvent.setup();
    render(<OnboardingWizard />);

    await user.click(
      screen.getByRole('button', {
        name: /i have a resume and a role in mind/i,
      })
    );

    expect(screen.getByTestId('identity-step-stub')).toBeInTheDocument();
  });

  it('shows a progress bar on Path A non-completion steps', async () => {
    const user = userEvent.setup();
    render(<OnboardingWizard />);

    await user.click(
      screen.getByRole('button', {
        name: /i have a resume and a role in mind/i,
      })
    );

    // The counter excludes path-chooser (§A4): Path A counts 5 steps and
    // identity is step 1 of 5. ProgressBar: Math.round((1/5)*100) = 20.
    const progressBar = screen.getByRole('progressbar');
    expect(progressBar).toHaveAttribute('aria-valuemax', '100');
    expect(progressBar).toHaveAttribute('aria-valuenow', '20');
    expect(screen.getByText('Step 1 of 5')).toBeInTheDocument();
  });

  it('advances through identity -> upload-resume -> add-job -> pick-targets -> completion', async () => {
    const user = userEvent.setup();
    render(<OnboardingWizard />);

    await user.click(
      screen.getByRole('button', {
        name: /i have a resume and a role in mind/i,
      })
    );

    // Step 1: identity -> next
    await user.click(screen.getByRole('button', { name: 'identity-complete' }));
    expect(screen.getByTestId('resume-uploader-stub')).toBeInTheDocument();

    // Step 2: upload-resume -> next
    await user.click(screen.getByRole('button', { name: 'resume-complete' }));
    expect(screen.getByTestId('job-url-input-stub')).toBeInTheDocument();

    // Step 3: add-job -> next
    await user.click(screen.getByRole('button', { name: 'job-complete' }));
    expect(screen.getByTestId('target-suggestions-stub')).toBeInTheDocument();

    // Step 4: pick-targets -> next
    await user.click(screen.getByRole('button', { name: 'targets-complete' }));
    expect(screen.getByTestId('completion-screen-stub')).toBeInTheDocument();
  });

  it('hides the progress bar on the completion step', async () => {
    const user = userEvent.setup();
    render(<OnboardingWizard />);

    await user.click(
      screen.getByRole('button', {
        name: /i have a resume and a role in mind/i,
      })
    );
    await user.click(screen.getByRole('button', { name: 'identity-complete' }));
    await user.click(screen.getByRole('button', { name: 'resume-complete' }));
    await user.click(screen.getByRole('button', { name: 'job-complete' }));
    await user.click(screen.getByRole('button', { name: 'targets-complete' }));

    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
  });
});

describe('OnboardingWizard — Path B (resume only)', () => {
  it('dispatches to IdentityStep and shows a progress bar', async () => {
    const user = userEvent.setup();
    render(<OnboardingWizard />);

    await user.click(
      screen.getByRole('button', {
        name: /i have a resume but i'm exploring roles/i,
      })
    );

    expect(screen.getByTestId('identity-step-stub')).toBeInTheDocument();
    // The counter excludes path-chooser (§A4): Path B counts 4 steps and
    // identity is step 1 of 4. ProgressBar: Math.round((1/4)*100) = 25.
    const progressBar = screen.getByRole('progressbar');
    expect(progressBar).toHaveAttribute('aria-valuemax', '100');
    expect(progressBar).toHaveAttribute('aria-valuenow', '25');
    expect(screen.getByText('Step 1 of 4')).toBeInTheDocument();
  });

  it('skips the add-job step and goes directly to pick-targets', async () => {
    const user = userEvent.setup();
    render(<OnboardingWizard />);

    await user.click(
      screen.getByRole('button', {
        name: /i have a resume but i'm exploring roles/i,
      })
    );
    await user.click(screen.getByRole('button', { name: 'identity-complete' }));
    await user.click(screen.getByRole('button', { name: 'resume-complete' }));

    expect(screen.getByTestId('target-suggestions-stub')).toBeInTheDocument();
    expect(screen.queryByTestId('job-url-input-stub')).not.toBeInTheDocument();
  });
});

describe('OnboardingWizard — Path C (conversation)', () => {
  it('dispatches to IdentityStep after selecting Path C', async () => {
    const user = userEvent.setup();
    render(<OnboardingWizard />);

    await user.click(
      screen.getByRole('button', { name: /i'm not sure where to start/i })
    );

    expect(screen.getByTestId('identity-step-stub')).toBeInTheDocument();
  });

  it('advances from identity -> conversation -> pick-targets -> completion', async () => {
    const user = userEvent.setup();
    render(<OnboardingWizard />);

    await user.click(
      screen.getByRole('button', { name: /i'm not sure where to start/i })
    );

    await user.click(screen.getByRole('button', { name: 'identity-complete' }));
    expect(screen.getByTestId('conversation-chat-stub')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'chat-complete' }));
    expect(screen.getByTestId('target-suggestions-stub')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'targets-complete' }));
    expect(screen.getByTestId('completion-screen-stub')).toBeInTheDocument();
  });
});
