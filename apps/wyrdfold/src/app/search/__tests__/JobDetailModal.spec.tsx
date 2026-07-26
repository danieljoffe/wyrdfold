import React from 'react';
import '@testing-library/jest-dom';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import JobDetailModal from '../JobDetailModal';
import type { JobSearchResult, TargetRef } from '../types';
import type { TargetsSource } from '../useTargetsSource';

// next/link needs the app-router context; stub to a plain anchor (LinkButton —
// the match/tailor CTAs — renders through next/link).
jest.mock('next/link', () => ({
  __esModule: true,
  default: ({
    href,
    children,
    ...rest
  }: {
    href: string;
    children: React.ReactNode;
  }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

const mockToast = jest.fn();
jest.mock('@/state/Toast/ToastProvider', () => ({
  useToast: () => ({ toast: mockToast }),
}));

// Funnel beacon (§10 PR6): assert the tick, never the wire.
const mockEmitSearchEvent = jest.fn();
jest.mock('../searchEvents', () => ({
  emitSearchEvent: (...args: unknown[]) => mockEmitSearchEvent(...args),
}));

const ORIGINAL_FETCH = global.fetch;

function result(overrides: Partial<JobSearchResult> = {}): JobSearchResult {
  return {
    id: '1',
    title: 'Frontend Engineer',
    company_name: 'Acme',
    location: 'Remote',
    department: null,
    salary_text: '$150k',
    absolute_url: 'https://ext.example/1',
    first_seen_at: null,
    created_at: null,
    snippet: null,
    ...overrides,
  };
}

/** Targets already loaded — the picker renders its menu without a fetch. */
function loadedTargets(
  targets: { id: string; label: string }[] = [
    { id: 't1', label: 'Frontend Engineer' },
  ]
): TargetsSource {
  return { targets, loading: false, error: null, ensureLoaded: jest.fn() };
}

function renderModal(
  opts: {
    job?: JobSearchResult;
    inTargets?: TargetRef[];
    isAuthenticated?: boolean;
    targetsSource?: TargetsSource;
    onClose?: () => void;
    onAddedToTarget?: (target: TargetRef) => void;
  } = {}
) {
  return render(
    <JobDetailModal
      job={opts.job ?? result()}
      inTargets={opts.inTargets ?? []}
      // Default authed — the existing suite covers the bind→unlock states; the
      // logged-out block below passes `isAuthenticated={false}` explicitly.
      isAuthenticated={opts.isAuthenticated ?? true}
      targetsSource={opts.targetsSource ?? loadedTargets()}
      onClose={opts.onClose ?? jest.fn()}
      onAddedToTarget={opts.onAddedToTarget}
    />
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  global.fetch = jest.fn() as unknown as typeof fetch;
});
afterAll(() => {
  global.fetch = ORIGINAL_FETCH;
});

describe('JobDetailModal', () => {
  it('renders the header, source link, and chips for both states', () => {
    renderModal({ job: result({ salary_text: null }) });

    expect(
      screen.getByRole('heading', { name: 'Frontend Engineer' })
    ).toBeInTheDocument();
    // Source link on its own line, new tab.
    const source = screen.getByRole('link', { name: /view original posting/i });
    expect(source).toHaveAttribute('href', 'https://ext.example/1');
    expect(source).toHaveAttribute('target', '_blank');
    // Chips: salary fallback + location.
    expect(screen.getByText('Salary not listed')).toBeInTheDocument();
    expect(screen.getByText('Remote')).toBeInTheDocument();
  });

  // ---- Unbound: the two BIND actions + the hint (no match/tailor) ----------

  it('unbound → shows add/create + the unlock hint, and hides match/tailor', () => {
    renderModal({ inTargets: [] });

    expect(
      screen.getByText(/add to a target to unlock llm pipelines/i)
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /add to target/i })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /create a target from this role/i })
    ).toBeInTheDocument();

    // Match/tailor are target-level — not shown until the listing is bound.
    expect(
      screen.queryByRole('link', { name: /see how you match/i })
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('link', { name: /tailor a r/i })
    ).not.toBeInTheDocument();
  });

  it('unbound → creates a target from the listing via the from-url flow', async () => {
    const created = {
      user_target: { id: 'ut1' },
      target: { id: 't1', label: 'Frontend Engineer' },
      was_matched: false,
    };
    (global.fetch as jest.Mock).mockResolvedValue({
      ok: true,
      status: 201,
      json: async () => created,
    });

    renderModal({ inTargets: [] });
    fireEvent.click(
      screen.getByRole('button', { name: /create a target from this role/i })
    );

    await waitFor(() =>
      expect(global.fetch).toHaveBeenCalledWith(
        '/api/targets/from-url',
        expect.objectContaining({ method: 'POST' })
      )
    );
    const call = (global.fetch as jest.Mock).mock.calls.find(
      c => typeof c[0] === 'string' && c[0].includes('/api/targets/from-url')
    );
    expect(call?.[1]?.body).toContain('https://ext.example/1'); // jd_url = the listing
    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: 'success' })
      )
    );
  });

  it('unbound → surfaces an error toast when create-target fails (no profile)', async () => {
    const payload = { detail: 'no prose doc to derive from' };
    (global.fetch as jest.Mock).mockResolvedValue({
      ok: false,
      status: 422,
      json: async () => payload,
      clone: () => ({ json: async () => payload }), // extractApiError reads via clone()
    });

    renderModal({ inTargets: [] });
    fireEvent.click(
      screen.getByRole('button', { name: /create a target from this role/i })
    );

    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: 'error' })
      )
    );
  });

  it('unbound → adds the listing to a chosen target via the picker', async () => {
    (global.fetch as jest.Mock).mockImplementation((url: string) => {
      if (typeof url === 'string' && url.includes('/add-to-target'))
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({
            success: true,
            job_posting_id: '1',
            target_id: 't1',
            score: 70,
          }),
        });
      return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
    });

    renderModal({ inTargets: [] });
    fireEvent.click(screen.getByRole('button', { name: /add to target/i }));
    fireEvent.click(
      await screen.findByRole('menuitem', { name: 'Frontend Engineer' })
    );

    await waitFor(() =>
      expect(global.fetch).toHaveBeenCalledWith(
        '/api/jobs/1/add-to-target',
        expect.objectContaining({ method: 'POST' })
      )
    );
    const addCall = (global.fetch as jest.Mock).mock.calls.find(
      c => typeof c[0] === 'string' && c[0].includes('/add-to-target')
    );
    expect(addCall?.[1]?.body).toContain('t1'); // target_id in the body
    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: 'success' })
      )
    );
  });

  it('unbound → notifies onAddedToTarget after a pick (drives the live unlock)', async () => {
    (global.fetch as jest.Mock).mockImplementation((url: string) => {
      if (typeof url === 'string' && url.includes('/add-to-target'))
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({
            success: true,
            job_posting_id: '1',
            target_id: 't1',
            score: 70,
          }),
        });
      return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
    });

    const onAddedToTarget = jest.fn();
    renderModal({ inTargets: [], onAddedToTarget });
    fireEvent.click(screen.getByRole('button', { name: /add to target/i }));
    fireEvent.click(
      await screen.findByRole('menuitem', { name: 'Frontend Engineer' })
    );

    // The membership shape ({ target_id, label }) the container needs to flip
    // this modal to bound + badge the card.
    await waitFor(() =>
      expect(onAddedToTarget).toHaveBeenCalledWith({
        target_id: 't1',
        label: 'Frontend Engineer',
      })
    );
  });

  // ---- Bound: the LLM actions UNLOCK (links to the matched surfaces) --------

  it('bound → shows the match + tailor links (to /jobs/[id]) and add-to-another', () => {
    renderModal({
      job: result({ id: '42' }),
      inTargets: [{ target_id: 't9', label: 'Frontend Roles' }],
    });

    // The pipeline-state line ("✓ In "Frontend Roles"").
    expect(screen.getByText(/Frontend Roles/)).toBeInTheDocument();

    // Match + tailor are LINKS to the existing matched surfaces (deliberate reuse).
    expect(
      screen.getByRole('link', { name: /see how you match/i })
    ).toHaveAttribute('href', '/jobs/42');
    expect(screen.getByRole('link', { name: /tailor a r/i })).toHaveAttribute(
      'href',
      '/jobs/42/resume'
    );

    // "Add to another target" (free) — and NO create-target / unlock hint.
    expect(
      screen.getByRole('button', { name: /add to another target/i })
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /create a target from this role/i })
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/unlock llm pipelines/i)).not.toBeInTheDocument();
  });

  it('closes via the dialog ✕ (shared-ui Modal), calling onClose', () => {
    const onClose = jest.fn();
    renderModal({ onClose });
    fireEvent.click(screen.getByRole('button', { name: /close dialog/i }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  // ---- Logged-out: info + source link + ONE soft allusion (#467 §11.5) -----

  it('logged-out → shows the source link + the signup allusion (link to /login)', () => {
    renderModal({ isAuthenticated: false });

    // Browsing stays free: header + source link.
    expect(
      screen.getByRole('heading', { name: 'Frontend Engineer' })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('link', { name: /view original posting/i })
    ).toHaveAttribute('href', 'https://ext.example/1');

    // The soft allusion — alludes to match + tailor, links to signup.
    expect(
      screen.getByText(/how this role matches their profile/i)
    ).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /sign up free/i })).toHaveAttribute(
      'href',
      '/login'
    );
  });

  it('logged-out → clicking "Sign up free" emits the signup_click conversion tick', () => {
    mockEmitSearchEvent.mockClear();
    renderModal({ isAuthenticated: false });
    fireEvent.click(screen.getByRole('link', { name: /sign up free/i }));
    expect(mockEmitSearchEvent).toHaveBeenCalledWith({
      event_type: 'signup_click',
      surface: 'public',
      job_posting_id: result().id,
    });
  });

  it('logged-out → hides EVERY bind / LLM action (even when passed a bound target)', () => {
    // Defence-in-depth: even if a stray `inTargets` were passed for a logged-out
    // render, the public detail must never expose the authed actions.
    renderModal({
      isAuthenticated: false,
      inTargets: [{ target_id: 't9', label: 'Frontend Roles' }],
    });

    expect(
      screen.queryByRole('button', { name: /add to target/i })
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /create a target from this role/i })
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('link', { name: /see how you match/i })
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('link', { name: /tailor a r/i })
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/unlock llm pipelines/i)).not.toBeInTheDocument();
  });
});
