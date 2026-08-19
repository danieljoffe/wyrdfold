import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import TargetSuggestions from '../TargetSuggestions';
import type { JobTarget, MatchedSuggestion } from '@/app/(app)/targets/types';

const fetchMock = jest.fn();
global.fetch = fetchMock as unknown as typeof fetch;

const mockPush = jest.fn();
jest.mock('next/navigation', () => ({
  useRouter: () => ({ push: mockPush, prefetch: jest.fn() }),
}));

beforeEach(() => {
  fetchMock.mockReset();
  mockPush.mockReset();
  // The component caches fetched suggestions per tab — without this,
  // one test's successful fetch feeds the next test's mount.
  sessionStorage.clear();
});

afterEach(() => {
  jest.useRealTimers();
});

function makeTarget(over: Partial<JobTarget> = {}): JobTarget {
  return {
    id: 't1',
    label: 'Senior Engineer',
    description: null,
    normalized_label: null,
    scoring_profile: {
      categories: {},
      seniority: { level: null, signals: [] },
      domain: { signals: [], weight: 0.5 },
      negative: { keywords: [], weight: -10 },
    },
    search_keywords: [],
    activation_status: 'active',
    profile_version: 1,
    app_active: true,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...over,
  };
}

function makeSuggestion(
  label: string,
  isNew = true,
  matched: JobTarget | null = null
): MatchedSuggestion {
  return {
    suggestion: {
      label,
      description: `${label} description`,
      core_skills: ['TypeScript', 'React'],
    },
    matched_target: matched,
    is_new: isNew,
  };
}

describe('TargetSuggestions — Path A (jobData provided)', () => {
  it('renders a loading spinner while auto-creating from posting', () => {
    // never resolves — exercises the loading state
    fetchMock.mockReturnValueOnce(
      new Promise(() => {
        // intentionally empty
      })
    );
    render(
      <TargetSuggestions
        onComplete={jest.fn()}
        onSkip={jest.fn()}
        jobData={{ postingId: 'p1', title: 'Eng', descriptionHtml: null }}
      />
    );
    expect(
      screen.getByText(/setting up a target from your job posting/i)
    ).toBeInTheDocument();
  });

  it('shows the created-target card when from-posting succeeds', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ label: 'Senior Engineer' }),
    });
    render(
      <TargetSuggestions
        onComplete={jest.fn()}
        onSkip={jest.fn()}
        jobData={{ postingId: 'p1', title: 'Eng', descriptionHtml: null }}
      />
    );

    await waitFor(() => {
      expect(screen.getByText(/target created/i)).toBeInTheDocument();
    });
    expect(screen.getByText('Senior Engineer')).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/targets/from-posting/p1',
      expect.objectContaining({ method: 'POST' })
    );
  });

  it('drafts the tailored resume and finishes on its review page (Path A payoff)', async () => {
    fetchMock.mockImplementation((url: string, init?: { method?: string }) => {
      const u = String(url);
      if (u.includes('/from-posting/')) {
        return Promise.resolve({
          ok: true,
          json: async () => ({ id: 't1', label: 'Senior Engineer' }),
        });
      }
      if (u.includes('/activate')) {
        return Promise.resolve({ ok: true, json: async () => ({}) });
      }
      if (u.includes('/tailor/resume') && init?.method === 'POST') {
        return Promise.resolve({
          ok: true,
          json: async () => ({ record: { id: 'r1' } }),
        });
      }
      if (u.includes('/onboarding/complete')) {
        return Promise.resolve({ ok: true, json: async () => ({}) });
      }
      // Everything else — including GET /api/jobs/p1 — 404s, exactly like
      // prod: the posting has no score row yet, so the detail endpoint
      // can't serve it. The payoff must not depend on it.
      return Promise.resolve({
        ok: false,
        status: 404,
        json: async () => ({}),
      });
    });
    const onComplete = jest.fn();

    render(
      <TargetSuggestions
        onComplete={onComplete}
        onSkip={jest.fn()}
        jobData={{
          postingId: 'p1',
          title: 'Eng',
          descriptionHtml: '<p>Great job</p>',
        }}
      />
    );

    await waitFor(() => {
      expect(mockPush).toHaveBeenCalledWith('/jobs/p1/resume');
    });
    // The wizard's own completion flow is bypassed — the review page IS
    // the completion.
    expect(onComplete).not.toHaveBeenCalled();
    const urls = (fetchMock.mock.calls as unknown[][]).map(([u]) => String(u));
    // The onboarding-complete flag was confirmed before navigating (the
    // redirect-loop guard).
    expect(urls.some(u => u.includes('/onboarding/complete'))).toBe(true);
    // The JD travels via jobData — no detail fetch (it would 404 until
    // the background activation scores the posting).
    expect(urls).not.toContain('/api/jobs/p1');
    // The tailor kick used the threaded JD.
    const tailorCall = (fetchMock.mock.calls as unknown[][]).find(([u]) =>
      String(u).includes('/tailor/resume')
    );
    expect(
      JSON.parse((tailorCall?.[1] as { body: string }).body)
    ).toMatchObject({
      job_description: '<p>Great job</p>',
      job_posting_id: 'p1',
    });
  });

  it('skips the resume draft entirely when no JD was extracted', async () => {
    fetchMock.mockImplementation((url: string) => {
      const u = String(url);
      if (u.includes('/from-posting/')) {
        return Promise.resolve({
          ok: true,
          json: async () => ({ id: 't1', label: 'Senior Engineer' }),
        });
      }
      if (u.includes('/activate')) {
        return Promise.resolve({ ok: true, json: async () => ({}) });
      }
      return Promise.resolve({
        ok: false,
        status: 404,
        json: async () => ({}),
      });
    });
    const onComplete = jest.fn();

    render(
      <TargetSuggestions
        onComplete={onComplete}
        onSkip={jest.fn()}
        jobData={{ postingId: 'p1', title: 'Eng', descriptionHtml: null }}
      />
    );

    await waitFor(
      () => {
        expect(onComplete).toHaveBeenCalled();
      },
      { timeout: 4000 }
    );
    // No JD → no tailor kick and no navigation; the normal completion
    // flow takes over.
    const urls = (fetchMock.mock.calls as unknown[][]).map(([u]) => String(u));
    expect(urls.some(u => u.includes('/tailor/resume'))).toBe(false);
    expect(mockPush).not.toHaveBeenCalled();
  });

  it('falls back to the completion flow when the resume draft fails', async () => {
    fetchMock.mockImplementation((url: string) => {
      const u = String(url);
      if (u.includes('/from-posting/')) {
        return Promise.resolve({
          ok: true,
          json: async () => ({ id: 't1', label: 'Senior Engineer' }),
        });
      }
      // Everything downstream (the tailor kick) fails — e.g. LLM
      // budget or gap gate.
      return Promise.resolve({
        ok: false,
        status: 429,
        json: async () => ({}),
      });
    });
    const onComplete = jest.fn();

    render(
      <TargetSuggestions
        onComplete={onComplete}
        onSkip={jest.fn()}
        jobData={{
          postingId: 'p1',
          title: 'Eng',
          descriptionHtml: '<p>Great job</p>',
        }}
      />
    );

    // The target still landed and the card shows; no navigation happened.
    await waitFor(() => {
      expect(screen.getByText(/target created/i)).toBeInTheDocument();
    });
    await waitFor(
      () => {
        expect(onComplete).toHaveBeenCalled();
      },
      { timeout: 4000 }
    );
    expect(mockPush).not.toHaveBeenCalled();
  });

  it('shows the error alert when from-posting fails', async () => {
    // First call (Path A) fails. The component then falls through to the
    // suggestions branch — its useEffect early-returns when jobData is set,
    // so no second call fires. The fallback "Set up your job targets"
    // manual prompt is rendered with the error alert above it.
    fetchMock.mockResolvedValueOnce({ ok: false, status: 500 });
    render(
      <TargetSuggestions
        onComplete={jest.fn()}
        onSkip={jest.fn()}
        jobData={{ postingId: 'p1', title: 'Eng', descriptionHtml: null }}
      />
    );

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(
        /could not auto-create target/i
      );
    });
  });
});

describe('TargetSuggestions — Path B/C (no jobData)', () => {
  it('fetches /api/targets/suggest and renders the suggestion cards', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        matches: [
          makeSuggestion('Frontend Engineer', true, null),
          makeSuggestion('Staff Engineer', false, makeTarget({ id: 't2' })),
        ],
      }),
    });

    render(<TargetSuggestions onComplete={jest.fn()} onSkip={jest.fn()} />);

    await waitFor(() => {
      expect(
        screen.getByRole('heading', { level: 2, name: /suggested targets/i })
      ).toBeInTheDocument();
    });

    // Cards rendered as role=checkbox, pre-selected by default
    const frontend = screen.getByRole('checkbox', {
      name: 'Frontend Engineer',
    });
    expect(frontend).toHaveAttribute('aria-checked', 'true');
    const staff = screen.getByRole('checkbox', { name: 'Staff Engineer' });
    expect(staff).toHaveAttribute('aria-checked', 'true');
  });

  it("renders an 'Existing' badge for non-new suggestions", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        matches: [
          makeSuggestion('Staff Engineer', false, makeTarget({ id: 't2' })),
        ],
      }),
    });
    render(<TargetSuggestions onComplete={jest.fn()} onSkip={jest.fn()} />);
    await waitFor(() => {
      expect(screen.getByText(/existing/i)).toBeInTheDocument();
    });
  });

  it('toggles selection when a suggestion card is clicked', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        matches: [makeSuggestion('Frontend Engineer', true, null)],
      }),
    });
    const user = userEvent.setup();
    render(<TargetSuggestions onComplete={jest.fn()} onSkip={jest.fn()} />);

    const card = await screen.findByRole('checkbox', {
      name: 'Frontend Engineer',
    });
    expect(card).toHaveAttribute('aria-checked', 'true'); // pre-selected
    await user.click(card);
    await waitFor(() => {
      expect(card).toHaveAttribute('aria-checked', 'false');
    });
    // Button copy reflects 0 selected
    expect(
      screen.getByRole('button', { name: /continue without targets/i })
    ).toBeInTheDocument();
  });

  it('renders singular vs plural button labels based on selection size', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        matches: [
          makeSuggestion('A'),
          makeSuggestion('B'),
          makeSuggestion('C'),
        ],
      }),
    });
    const user = userEvent.setup();
    render(<TargetSuggestions onComplete={jest.fn()} onSkip={jest.fn()} />);

    // All 3 pre-selected
    expect(
      await screen.findByRole('button', { name: /create 3 targets/i })
    ).toBeInTheDocument();

    await user.click(screen.getByRole('checkbox', { name: 'A' }));
    await user.click(screen.getByRole('checkbox', { name: 'B' }));

    await waitFor(() => {
      expect(
        screen.getByRole('button', { name: /create 1 target/i })
      ).toBeInTheDocument();
    });
  });

  it('renders the manual fallback when /suggest returns zero matches', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ matches: [] }),
    });
    render(<TargetSuggestions onComplete={jest.fn()} onSkip={jest.fn()} />);

    await waitFor(() => {
      expect(
        screen.getByRole('heading', {
          level: 2,
          name: /set up your job targets/i,
        })
      ).toBeInTheDocument();
    });
    expect(
      screen.getByRole('button', { name: /create your first target/i })
    ).toBeInTheDocument();
  });

  it('renders an error alert when /suggest fails', async () => {
    fetchMock.mockResolvedValueOnce({ ok: false, status: 500 });
    render(<TargetSuggestions onComplete={jest.fn()} onSkip={jest.fn()} />);

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(
        /could not generate suggestions/i
      );
    });
  });

  it('kicks off /activate for each created target so jobs start polling', async () => {
    // Regression test for the "onboarded targets stuck at activation_status=idle"
    // bug. Without the activate kickoff, the derive→poll pipeline never runs
    // and the user lands at /jobs with no postings — even after a full
    // onboarding round-trip.
    fetchMock.mockImplementation((url: string) => {
      // /suggest returns two new suggestions
      if (typeof url === 'string' && url.endsWith('/api/targets/suggest')) {
        return Promise.resolve({
          ok: true,
          json: async () => ({
            matches: [
              makeSuggestion('Frontend Engineer', true, null),
              makeSuggestion('Backend Engineer', true, null),
            ],
          }),
        });
      }
      // /api/targets POST returns the created target with an id
      if (typeof url === 'string' && url === '/api/targets') {
        return Promise.resolve({
          ok: true,
          json: async () =>
            makeTarget({ id: `t-${Math.random().toString(36).slice(2, 6)}` }),
        });
      }
      // /link POST succeeds
      // /activate POST succeeds — but we only care that it was CALLED.
      return Promise.resolve({ ok: true, json: async () => ({}) });
    });

    const user = userEvent.setup();
    render(<TargetSuggestions onComplete={jest.fn()} onSkip={jest.fn()} />);

    await waitFor(() => {
      expect(
        screen.getByRole('heading', { level: 2, name: /suggested targets/i })
      ).toBeInTheDocument();
    });

    await user.click(screen.getByRole('button', { name: /create 2 targets/i }));

    // After create+link, /activate must fire for both targets — the bug
    // was that this call was missing, leaving the activation pipeline
    // (derive → poll) un-kicked.
    await waitFor(() => {
      const activateCalls = fetchMock.mock.calls.filter(
        ([url, init]: [string, RequestInit | undefined]) =>
          typeof url === 'string' &&
          /\/api\/targets\/[^/]+\/activate$/.test(url) &&
          init?.method === 'POST'
      );
      expect(activateCalls.length).toBe(2);
    });
  });

  it('surfaces the refusal instead of advancing when every link is rejected', async () => {
    // #857 / #864: the cap 409 and the trial 402 were both swallowed by a
    // bare `catch {}`, and the wizard advanced to "You're all set!" — the
    // user asked for targets, got none, and was told it worked.
    const onComplete = jest.fn();
    fetchMock.mockImplementation((url: string) => {
      if (typeof url === 'string' && url.endsWith('/api/targets/suggest')) {
        return Promise.resolve({
          ok: true,
          json: async () => ({
            matches: [makeSuggestion('Frontend Engineer', true, null)],
          }),
        });
      }
      if (typeof url === 'string' && url === '/api/targets') {
        return Promise.resolve({
          ok: true,
          json: async () => makeTarget({ id: 't-1' }),
        });
      }
      if (typeof url === 'string' && /\/link$/.test(url)) {
        const detail = "You're on Starter, which allows 2 active targets.";
        return Promise.resolve({
          ok: false,
          status: 409,
          clone: () => ({ json: async () => ({ detail }) }),
          json: async () => ({ detail }),
        });
      }
      return Promise.resolve({ ok: true, json: async () => ({}) });
    });

    const user = userEvent.setup();
    render(<TargetSuggestions onComplete={onComplete} onSkip={jest.fn()} />);

    await waitFor(() => {
      expect(
        screen.getByRole('heading', { level: 2, name: /suggested targets/i })
      ).toBeInTheDocument();
    });
    await user.click(screen.getByRole('button', { name: /create 1 target/i }));

    // The API's own message, verbatim — it names the cap and the plan.
    expect(
      await screen.findByText(/allows 2 active targets/i)
    ).toBeInTheDocument();

    // And it must NOT sail on to the completion screen. Waiting past the
    // 1500ms auto-advance so this can't pass by simply being early.
    await new Promise(r => setTimeout(r, 1800));
    expect(onComplete).not.toHaveBeenCalled();
  });

  it('reports the created count via onTargetsCreated (completion-copy input)', async () => {
    // CompletionScreen branches its copy on this number (sweep
    // 2026-08-14 P2): a zero-target finish must not claim "all set".
    fetchMock.mockImplementation((url: string) => {
      if (typeof url === 'string' && url.endsWith('/api/targets/suggest')) {
        return Promise.resolve({
          ok: true,
          json: async () => ({
            matches: [
              makeSuggestion('Frontend Engineer', true, null),
              makeSuggestion('Backend Engineer', true, null),
            ],
          }),
        });
      }
      if (typeof url === 'string' && url === '/api/targets') {
        return Promise.resolve({
          ok: true,
          json: async () =>
            makeTarget({ id: `t-${Math.random().toString(36).slice(2, 6)}` }),
        });
      }
      return Promise.resolve({ ok: true, json: async () => ({}) });
    });

    const onTargetsCreated = jest.fn();
    const user = userEvent.setup();
    render(
      <TargetSuggestions
        onComplete={jest.fn()}
        onSkip={jest.fn()}
        onTargetsCreated={onTargetsCreated}
      />
    );

    await waitFor(() => {
      expect(
        screen.getByRole('heading', { level: 2, name: /suggested targets/i })
      ).toBeInTheDocument();
    });

    // Deselect one of the two pre-selected suggestions, then create —
    // the reported count must reflect what was actually created (1),
    // not what was offered (2).
    await user.click(screen.getByText('Backend Engineer'));
    await user.click(screen.getByRole('button', { name: /create 1 target/i }));

    await waitFor(() => expect(onTargetsCreated).toHaveBeenCalledWith(1));
  });

  it('reports zero when the user continues without targets', async () => {
    fetchMock.mockImplementation((url: string) => {
      if (typeof url === 'string' && url.endsWith('/api/targets/suggest')) {
        return Promise.resolve({
          ok: true,
          json: async () => ({
            matches: [makeSuggestion('Frontend Engineer', true, null)],
          }),
        });
      }
      return Promise.resolve({ ok: true, json: async () => ({}) });
    });

    const onTargetsCreated = jest.fn();
    const onComplete = jest.fn();
    const user = userEvent.setup();
    render(
      <TargetSuggestions
        onComplete={onComplete}
        onSkip={jest.fn()}
        onTargetsCreated={onTargetsCreated}
      />
    );

    await waitFor(() => {
      expect(
        screen.getByRole('heading', { level: 2, name: /suggested targets/i })
      ).toBeInTheDocument();
    });

    await user.click(screen.getByText('Frontend Engineer')); // deselect
    await user.click(
      screen.getByRole('button', { name: /continue without targets/i })
    );

    // Zero-selection short-circuits straight to onComplete — no target
    // writes, no creation report (the wizard's count stays at its 0
    // default, which is exactly what CompletionScreen should see).
    await waitFor(() => expect(onComplete).toHaveBeenCalled());
    expect(onTargetsCreated).not.toHaveBeenCalled();
    const targetPosts = fetchMock.mock.calls.filter(
      ([url, init]: [string, RequestInit | undefined]) =>
        url === '/api/targets' && init?.method === 'POST'
    );
    expect(targetPosts.length).toBe(0);
  });

  it('invokes onSkip when "Skip this step" is clicked from the manual fallback', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ matches: [] }),
    });
    const onSkip = jest.fn();
    const user = userEvent.setup();
    render(<TargetSuggestions onComplete={jest.fn()} onSkip={onSkip} />);

    const skip = await screen.findByRole('button', { name: /skip this step/i });
    await user.click(skip);
    expect(onSkip).toHaveBeenCalledTimes(1);
  });
});

describe('TargetSuggestions — per-tab suggestion cache (sweep 2026-08-14 A3)', () => {
  const CACHE_KEY = 'wyrdfold.onboarding.suggestions';

  const seedCache = (labels: string[], cachedAt = Date.now()) => {
    sessionStorage.setItem(
      CACHE_KEY,
      JSON.stringify({
        cachedAt,
        matches: labels.map(l => makeSuggestion(l, true, null)),
      })
    );
  };

  beforeEach(() => {
    sessionStorage.clear();
  });

  it('serves the cached set on mount without a suggest POST', async () => {
    seedCache(['Frontend Engineer', 'Backend Engineer']);

    render(<TargetSuggestions onComplete={jest.fn()} onSkip={jest.fn()} />);

    // Cards render straight from the cache…
    expect(await screen.findByText('Frontend Engineer')).toBeInTheDocument();
    expect(screen.getByText('Backend Engineer')).toBeInTheDocument();
    // …with everything pre-selected, same as a fresh fetch…
    expect(
      screen.getByRole('button', { name: /create 2 targets/i })
    ).toBeInTheDocument();
    // …and NO billed suggest call fired.
    const suggestCalls = fetchMock.mock.calls.filter(([u]) =>
      String(u).endsWith('/api/targets/suggest')
    );
    expect(suggestCalls.length).toBe(0);
  });

  it('ignores an expired cache entry and fetches fresh', async () => {
    seedCache(['Stale Engineer'], Date.now() - 31 * 60 * 1000);
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        matches: [makeSuggestion('Fresh Engineer', true, null)],
      }),
    });

    render(<TargetSuggestions onComplete={jest.fn()} onSkip={jest.fn()} />);

    expect(await screen.findByText('Fresh Engineer')).toBeInTheDocument();
    expect(screen.queryByText('Stale Engineer')).not.toBeInTheDocument();
  });

  it('a successful suggest writes the cache', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        matches: [makeSuggestion('Platform Engineer', true, null)],
      }),
    });

    render(<TargetSuggestions onComplete={jest.fn()} onSkip={jest.fn()} />);

    expect(await screen.findByText('Platform Engineer')).toBeInTheDocument();
    const cached = JSON.parse(sessionStorage.getItem(CACHE_KEY) ?? 'null');
    expect(cached?.matches?.[0]?.suggestion?.label).toBe('Platform Engineer');
    expect(typeof cached?.cachedAt).toBe('number');
  });

  it('"Refresh suggestions" is a deliberate reroll: clears the cache and re-POSTs', async () => {
    seedCache(['Frontend Engineer']);
    fetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({
        matches: [makeSuggestion('Rerolled Engineer', true, null)],
      }),
    });
    const user = userEvent.setup();

    render(<TargetSuggestions onComplete={jest.fn()} onSkip={jest.fn()} />);
    expect(await screen.findByText('Frontend Engineer')).toBeInTheDocument();

    await user.click(
      screen.getByRole('button', { name: /refresh suggestions/i })
    );

    expect(await screen.findByText('Rerolled Engineer')).toBeInTheDocument();
    const suggestCalls = fetchMock.mock.calls.filter(([u]) =>
      String(u).endsWith('/api/targets/suggest')
    );
    expect(suggestCalls.length).toBe(1);
    // The rerolled set replaces the cached one.
    const cached = JSON.parse(sessionStorage.getItem(CACHE_KEY) ?? 'null');
    expect(cached?.matches?.[0]?.suggestion?.label).toBe('Rerolled Engineer');
  });

  it('creating targets consumes the cache', async () => {
    seedCache(['Frontend Engineer']);
    fetchMock.mockImplementation((url: string) => {
      if (typeof url === 'string' && url === '/api/targets') {
        return Promise.resolve({
          ok: true,
          json: async () => makeTarget({ id: 't-new' }),
        });
      }
      return Promise.resolve({ ok: true, json: async () => ({}) });
    });
    const user = userEvent.setup();

    render(<TargetSuggestions onComplete={jest.fn()} onSkip={jest.fn()} />);
    expect(await screen.findByText('Frontend Engineer')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /create 1 target/i }));

    await waitFor(() => expect(sessionStorage.getItem(CACHE_KEY)).toBeNull());
  });
});
