/**
 * Tests for the dashboard route (`page.tsx`) — server component that
 * gates new users into the onboarding wizard via the explicit
 * `onboarding_completed_at` flag, with a `hasProse` fallback.
 *
 * See plan-wyrdfold-onboarding-completion-tracking.md.
 */

const mockRedirect = jest.fn((target: string) => {
  // Match Next.js behaviour: `redirect()` throws to abort rendering.
  throw new Error(`REDIRECT:${target}`);
});

const mockFetch = jest.fn();
const mockSentryCapture = jest.fn();

jest.mock('next/navigation', () => ({
  redirect: (target: string) => mockRedirect(target),
}));

jest.mock('@/lib/api/proxy', () => ({
  fetchJsonFromWyrdfoldAPI: (...args: unknown[]) => mockFetch(...args),
}));

jest.mock('@sentry/nextjs', () => ({
  captureMessage: (msg: string, opts: unknown) => mockSentryCapture(msg, opts),
}));

// Re-import after mocks are set up.
import WyrdfoldDashboard from '../dashboard/page';

/** Walk a React element tree (children only — props can be circular) and
 *  report whether any text node contains the given string. Server
 *  components can't be DOM-rendered in jest, so gate tests assert on the
 *  returned element tree instead. */
function treeContainsText(node: unknown, text: string): boolean {
  if (typeof node === 'string') return node.includes(text);
  if (Array.isArray(node)) return node.some(n => treeContainsText(n, text));
  if (node && typeof node === 'object' && 'props' in node) {
    const props = (node as { props: { children?: unknown } }).props;
    return treeContainsText(props?.children, text);
  }
  return false;
}

describe('WyrdfoldDashboard route', () => {
  beforeEach(() => {
    mockRedirect.mockClear();
    mockFetch.mockClear();
    mockSentryCapture.mockClear();
  });

  it('redirects to /onboarding when the flag is null (brand-new user)', async () => {
    mockFetch.mockResolvedValueOnce({
      completed_at: null,
      path: null,
      current_step: null,
    });

    await expect(
      WyrdfoldDashboard({ searchParams: Promise.resolve({}) })
    ).rejects.toThrow('REDIRECT:/onboarding');

    expect(mockRedirect).toHaveBeenCalledWith('/onboarding');
    expect(mockFetch).toHaveBeenCalledWith('/profile/onboarding');
    // The widget fetches now START alongside the gate read (they no longer
    // queue behind its round-trip on every onboarded load), so this
    // pre-wizard visit fires them too — 5 calls, results discarded by the
    // redirect. Deliberate trade: a handful of one-time reads on the rarest
    // path buys a round-trip off every load of the app's default page.
    expect(mockFetch).toHaveBeenCalledTimes(5);
    expect(mockSentryCapture).not.toHaveBeenCalled();
  });

  it('does NOT redirect a deferred user — renders the resume-setup nudge instead', async () => {
    // "Finish setup later" sets deferred_at while completed_at stays
    // NULL (onboarding-sweep-2026-08-14 P1). Bouncing this user back
    // into the wizard would override their explicit exit; they get a
    // nudge banner and /onboarding remains enterable + resumable.
    mockFetch
      .mockResolvedValueOnce({
        completed_at: null,
        deferred_at: '2026-08-14T00:00:00Z',
        path: 'B',
        current_step: 'upload-resume',
      })
      .mockResolvedValueOnce({ postings: [], total: 0, page: 1, page_size: 5 })
      .mockResolvedValueOnce({ prose: null })
      .mockResolvedValue({ targets: [], postings: [], total: 0 });

    const result = await WyrdfoldDashboard({
      searchParams: Promise.resolve({}),
    });

    expect(mockRedirect).not.toHaveBeenCalled();
    expect(treeContainsText(result, 'Resume setup')).toBe(true);
  });

  it('does NOT show the nudge once onboarding is completed', async () => {
    // complete clears deferred_at server-side, but guard the render
    // condition anyway: a completed profile never sees the nudge even
    // if a stale deferred_at slips through.
    mockFetch
      .mockResolvedValueOnce({
        completed_at: '2026-06-01T00:00:00Z',
        deferred_at: '2026-08-14T00:00:00Z',
        path: 'B',
        current_step: 'completion',
      })
      .mockResolvedValueOnce({ postings: [], total: 0, page: 1, page_size: 5 })
      .mockResolvedValueOnce({
        id: 'p-1',
        content: 'My experience...',
        version: 1,
      })
      .mockResolvedValue({ targets: [], postings: [], total: 0 });

    const result = await WyrdfoldDashboard({
      searchParams: Promise.resolve({}),
    });

    expect(mockRedirect).not.toHaveBeenCalled();
    expect(treeContainsText(result, 'Resume setup')).toBe(false);
  });

  it('does NOT redirect when the onboarding read fails (null) — fails open', async () => {
    // A null *result* means the read itself failed (degraded API: auth
    // refresh race, network blip, upstream 5xx) — NOT "never onboarded".
    // The old behaviour redirected here, which bounced an already-
    // onboarded user into a loop on a single flaky read. We now fail open
    // and let the dashboard render its own graceful empty/setup states.
    mockFetch
      .mockResolvedValueOnce(null) // onboarding status read failed
      // Promise.all fallthrough: jobs, prose, targets, counts
      .mockResolvedValueOnce({ postings: [], total: 0, page: 1, page_size: 5 })
      .mockResolvedValueOnce({ prose: null })
      .mockResolvedValue({ targets: [], postings: [], total: 0 });

    const result = await WyrdfoldDashboard({
      searchParams: Promise.resolve({}),
    });

    expect(mockRedirect).not.toHaveBeenCalled();
    expect(result).toBeDefined();
  });

  it('renders the empty-state dashboard + emits Sentry warning when flag is set but prose is missing', async () => {
    // Data drift OR legitimate Path A/B user who skipped the resume
    // step (e.g. upload failed mid-flow). Surface to Sentry so we
    // notice; render the dashboard's existing empty state ("Set up
    // profile" CTA) rather than bouncing back to /onboarding — the
    // wizard restarts at path-chooser, so bouncing creates a redirect
    // loop for any user without prose.
    mockFetch
      .mockResolvedValueOnce({
        completed_at: '2026-06-01T00:00:00Z',
        path: 'A',
        current_step: 'completion',
      })
      // Then the Promise.all: jobs, prose (null), targets, ...counts
      .mockResolvedValueOnce({ postings: [], total: 0, page: 1, page_size: 5 })
      .mockResolvedValueOnce({ prose: null }) // ← prose missing
      .mockResolvedValue({ targets: [], postings: [], total: 0 });

    const result = await WyrdfoldDashboard({
      searchParams: Promise.resolve({}),
    });

    expect(mockSentryCapture).toHaveBeenCalledWith(
      'dashboard:onboarding_flag_set_but_no_prose',
      expect.objectContaining({ level: 'warning' })
    );
    expect(mockRedirect).not.toHaveBeenCalled();
    expect(result).toBeDefined();
  });

  it('renders the dashboard when the flag is set and prose exists', async () => {
    mockFetch
      .mockResolvedValueOnce({
        completed_at: '2026-06-01T00:00:00Z',
        path: 'A',
        current_step: 'completion',
      })
      .mockResolvedValueOnce({ postings: [], total: 0, page: 1, page_size: 5 })
      .mockResolvedValueOnce({
        id: 'p-1',
        content: 'My experience...',
        version: 1,
      })
      .mockResolvedValue({ targets: [], postings: [], total: 0 });

    const result = await WyrdfoldDashboard({
      searchParams: Promise.resolve({}),
    });

    expect(mockRedirect).not.toHaveBeenCalled();
    expect(mockSentryCapture).not.toHaveBeenCalled();
    expect(result).toBeDefined();
  });

  it('view=trends fetches the insights slices, not the launcher data', async () => {
    // onboarding gate read
    mockFetch.mockResolvedValueOnce({
      completed_at: '2026-07-01T00:00:00Z',
      path: 'A',
      current_step: null,
    });
    // three insights slices (pipeline / targets / skills-cost)
    mockFetch.mockResolvedValue(null);

    const result = await WyrdfoldDashboard({
      searchParams: Promise.resolve({ view: 'trends' }),
    });

    expect(result).toBeTruthy();
    const endpoints = mockFetch.mock.calls.map(c => c[0]);
    expect(endpoints).toContain('/insights/pipeline');
    expect(endpoints).toContain('/insights/targets');
    expect(endpoints).toContain('/insights/skills-cost');
    // The Today launcher's fetches must NOT fire on the trends view —
    // one section per request.
    expect(endpoints).not.toContain('/jobs/pipeline-counts');
    expect(endpoints).not.toContain('/experience/prose');
  });

  it('unknown view values fall back to the Today launcher', async () => {
    mockFetch.mockResolvedValueOnce({
      completed_at: '2026-07-01T00:00:00Z',
      path: 'A',
      current_step: null,
    });
    mockFetch.mockResolvedValue(null);

    await WyrdfoldDashboard({
      searchParams: Promise.resolve({ view: 'garbage' }),
    });

    const endpoints = mockFetch.mock.calls.map(c => c[0]);
    expect(endpoints).toContain('/jobs/pipeline-counts');
    expect(endpoints).not.toContain('/insights/pipeline');
  });
});
