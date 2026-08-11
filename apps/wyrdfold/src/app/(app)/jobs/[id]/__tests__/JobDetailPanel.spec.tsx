import React from 'react';
import '@testing-library/jest-dom';
import { act, render, screen, waitFor } from '@testing-library/react';
import JobDetailPanel from '../../JobDetailPanel';
import type { JobPosting } from '../../types';

const mockToast = jest.fn();
jest.mock('@/state/Toast/ToastProvider', () => ({
  useToast: () => ({ toast: mockToast }),
}));

jest.mock('next/navigation', () => ({
  useRouter: () => ({ push: jest.fn(), prefetch: jest.fn() }),
}));

// CoverLetterSection is exercised in its own spec; stub to avoid
// secondary fetches and keep this spec focused on the panel itself.
jest.mock('../../CoverLetterSection', () => ({
  __esModule: true,
  default: () => <div data-testid='cover-letter-section-stub' />,
}));

const ORIGINAL_FETCH = global.fetch;

function makeJob(overrides: Partial<JobPosting> = {}): JobPosting {
  return {
    id: 'j-1',
    external_id: 'ext-1',
    source_id: 'src-1',
    title: 'Senior Frontend Engineer',
    company_name: 'Acme',
    location: 'Remote',
    absolute_url: null,
    score: 82,
    score_breakdown: null,
    scoring_status: 'complete',
    status: 'new',
    salary_text: null,
    source_posted_at: null,

    cataloged_at: '2026-01-01',
    ...overrides,
  };
}

beforeEach(() => {
  jest.clearAllMocks();
  global.fetch = jest.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({ entries: [] }),
  }) as unknown as typeof fetch;
});

afterAll(() => {
  global.fetch = ORIGINAL_FETCH;
});

describe('JobDetailPanel', () => {
  it('renders the score badge with a labelled value', async () => {
    render(
      <JobDetailPanel
        posting={makeJob({ score: 88 })}
        targetId={undefined}
        viewFullHref={undefined}
        onDelete={undefined}
        onStatusChange={undefined}
      />
    );
    expect(await screen.findByLabelText(/match score 88/i)).toBeInTheDocument();
  });

  it('renders a score-breakdown skeleton when the posting has no breakdown', () => {
    render(
      <JobDetailPanel
        posting={makeJob({ score_breakdown: null })}
        targetId={undefined}
        viewFullHref={undefined}
        onDelete={undefined}
        onStatusChange={undefined}
      />
    );
    expect(screen.getByText(/score breakdown/i)).toBeInTheDocument();
  });

  it('renders one row per non-zero factor in the score breakdown (pending rows — graded rows show fit axes, #609)', () => {
    render(
      <JobDetailPanel
        posting={makeJob({
          // Keyword components are the PENDING band's breakdown; graded rows
          // render their fit axes instead (JobDetailPanelBreakdown.spec.tsx).
          pending: true,
          scoring_status: 'stage2',
          axis_scores: null,
          score_breakdown: {
            role_titles: 30,
            technologies: 20,
            negative: -5,
            domain_skills: 0,
          },
        })}
        targetId={undefined}
        viewFullHref={undefined}
        onDelete={undefined}
        onStatusChange={undefined}
      />
    );
    expect(screen.getByText(/role titles/i)).toBeInTheDocument();
    expect(screen.getByText(/technologies/i)).toBeInTheDocument();
    expect(screen.getByText(/penalties/i)).toBeInTheDocument();
    // Zero-valued factor must be hidden.
    expect(screen.queryByText(/domain skills/i)).not.toBeInTheDocument();
  });

  it('renders the "Open full view" link when viewFullHref is provided', () => {
    render(
      <JobDetailPanel
        posting={makeJob()}
        targetId={undefined}
        viewFullHref='/jobs/j-1'
        onDelete={undefined}
        onStatusChange={undefined}
      />
    );
    expect(
      screen.getByRole('link', { name: /open full view/i })
    ).toHaveAttribute('href', '/jobs/j-1');
  });

  it('hides the Delete button when hideDelete is set', () => {
    render(
      <JobDetailPanel
        posting={makeJob()}
        targetId={undefined}
        viewFullHref='/jobs/j-1'
        onDelete={undefined}
        onStatusChange={undefined}
        hideDelete
      />
    );
    expect(
      screen.queryByRole('button', { name: /^delete$/i })
    ).not.toBeInTheDocument();
  });

  it('renders the resume CTA whenever a target is selected, regardless of status', async () => {
    // ResumeSection's existence is now gated only by ``targetId`` (was
    // ``status === 'resume_draft' || 'resume_ready'``). A new user
    // opening a fresh ``new`` job needs the Generate CTA to be visible
    // without first discovering they must flip status. The status flip
    // itself happens on the backend persistence side-effect.
    global.fetch = jest.fn().mockImplementation((url: string) => {
      if (typeof url === 'string' && url.includes('/tailor/by-job/')) {
        // ResumeSection polls the tailored doc's state; a record inside
        // the #656 envelope means "Review tailored resume" link appears.
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({
            record: { id: 'r-1', approved_at: null },
            status: 'idle',
          }),
        });
      }
      if (typeof url === 'string' && url.includes('/api/jobs/analysis/')) {
        // ``targetId`` triggers an auto-fire LLM analysis useEffect.
        // Short-circuit with a non-200 so it sets analysisError once
        // and stops re-trying — the panel still renders the resume
        // section under it, which is what we're asserting.
        return Promise.resolve({
          ok: false,
          status: 503,
          json: async () => ({ detail: 'mocked off' }),
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({ entries: [] }),
      });
    }) as unknown as typeof fetch;

    render(
      <JobDetailPanel
        posting={makeJob({ status: 'resume_draft' })}
        targetId='t-1'
        viewFullHref={undefined}
        onDelete={undefined}
        onStatusChange={undefined}
      />
    );
    expect(
      await screen.findByRole('link', { name: /review tailored resume/i })
    ).toHaveAttribute('href', '/jobs/j-1/resume');
  });

  it('renders a "set up your profile" CTA (not a raw error / retry) when analysis returns a no_profile marker (#105)', async () => {
    // The panel auto-fires the LLM analysis on open. For a user without an
    // experience profile the backend returns a 200 empty-state marker
    // (``{code:'no_profile'}``) — a 200, not a 404, so the auto-fire doesn't
    // log a console error. The panel must render a setup CTA — never leak the
    // old "…POST /experience/derive first." dev message, and never offer a
    // "Retry analysis" that can't succeed.
    const noProfile = {
      code: 'no_profile',
      message: 'Set up your experience profile to generate a job-fit analysis.',
    };
    global.fetch = jest.fn().mockImplementation((url: string) => {
      if (typeof url === 'string' && url.includes('/api/jobs/analysis/')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => noProfile,
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({ entries: [] }),
      });
    }) as unknown as typeof fetch;

    render(
      <JobDetailPanel
        posting={makeJob({ status: 'new' })}
        targetId='t-1'
        viewFullHref={undefined}
        onDelete={undefined}
        onStatusChange={undefined}
      />
    );

    const cta = await screen.findByRole('link', {
      name: /set up your profile/i,
    });
    expect(cta).toHaveAttribute('href', '/profile');
    expect(screen.queryByText(/experience\/derive/i)).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /retry analysis/i })
    ).not.toBeInTheDocument();
  });

  // --- Non-blocking analysis: kick-off + poll (#459) ---------------------

  function analysisRecord() {
    return {
      id: 'a-1',
      job_posting_id: 'j-1',
      scorecard: {
        skills_matched: [],
        skills_missing: ['GraphQL'],
        nice_to_haves: [],
        seniority_fit: 'strong',
        seniority_rationale: 'Senior match.',
        domain_fit: 'moderate',
        domain_rationale: 'Adjacent domain.',
      },
      recommendation: 'Apply — strong React match.',
      model: 'claude-sonnet-4-6',
      cost_usd: 0.001,
      latency_ms: 50,
      created_at: '2026-01-01T00:00:00Z',
    };
  }

  it('renders the scorecard immediately when the kick-off returns a cached record', async () => {
    const record = analysisRecord();
    global.fetch = jest.fn().mockImplementation((url: string) => {
      if (typeof url === 'string' && url.includes('/api/jobs/analysis/')) {
        // Cache hit → the record comes straight back from the kick-off POST,
        // no polling.
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => record,
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({ entries: [] }),
      });
    }) as unknown as typeof fetch;

    render(
      <JobDetailPanel
        posting={makeJob()}
        targetId='t-1'
        viewFullHref={undefined}
        onDelete={undefined}
        onStatusChange={undefined}
      />
    );

    expect(
      await screen.findByText(/Apply — strong React match/i)
    ).toBeInTheDocument();
    expect(screen.getByText(/Seniority: strong/i)).toBeInTheDocument();
    expect(screen.getByText('GraphQL')).toBeInTheDocument();
  });

  it('shows the background message on a 202, then renders the scorecard once a poll returns the record (#459)', async () => {
    jest.useFakeTimers();
    try {
      const record = analysisRecord();
      let getPolls = 0;
      global.fetch = jest
        .fn()
        .mockImplementation((url: string, opts?: RequestInit) => {
          if (typeof url === 'string' && url.includes('/api/jobs/analysis/')) {
            if ((opts?.method ?? 'GET') === 'POST') {
              // Kick-off: cache miss → running (backgrounded).
              return Promise.resolve({
                ok: true,
                status: 202,
                json: async () => ({ status: 'running' }),
              });
            }
            // Poll: still running once, then the finished record.
            getPolls += 1;
            return Promise.resolve({
              ok: true,
              status: 200,
              json: async () =>
                getPolls === 1 ? { status: 'running' } : record,
            });
          }
          return Promise.resolve({
            ok: true,
            status: 200,
            json: async () => ({ entries: [] }),
          });
        }) as unknown as typeof fetch;

      render(
        <JobDetailPanel
          posting={makeJob()}
          targetId='t-1'
          viewFullHref={undefined}
          onDelete={undefined}
          onStatusChange={undefined}
        />
      );

      // While backgrounded, the panel reassures the user they can leave.
      expect(
        await screen.findByText(/runs in the background/i)
      ).toBeInTheDocument();

      // Walk the poll loop (2.5s cadence): first poll → running, second → record.
      await act(async () => {
        await jest.advanceTimersByTimeAsync(6000);
      });

      expect(
        await screen.findByText(/Apply — strong React match/i)
      ).toBeInTheDocument();
      // Poll GETs happened (at least the two we scripted).
      expect(getPolls).toBeGreaterThanOrEqual(2);
    } finally {
      jest.useRealTimers();
    }
  });

  it('surfaces a retry when a poll reports the background run failed (#459)', async () => {
    jest.useFakeTimers();
    try {
      global.fetch = jest
        .fn()
        .mockImplementation((url: string, opts?: RequestInit) => {
          if (typeof url === 'string' && url.includes('/api/jobs/analysis/')) {
            if ((opts?.method ?? 'GET') === 'POST') {
              return Promise.resolve({
                ok: true,
                status: 202,
                json: async () => ({ status: 'running' }),
              });
            }
            return Promise.resolve({
              ok: true,
              status: 200,
              json: async () => ({
                status: 'error',
                message: 'Analysis failed. Please retry.',
              }),
            });
          }
          return Promise.resolve({
            ok: true,
            status: 200,
            json: async () => ({ entries: [] }),
          });
        }) as unknown as typeof fetch;

      render(
        <JobDetailPanel
          posting={makeJob()}
          targetId='t-1'
          viewFullHref={undefined}
          onDelete={undefined}
          onStatusChange={undefined}
        />
      );

      await act(async () => {
        await jest.advanceTimersByTimeAsync(6000);
      });

      expect(
        await screen.findByText(/Analysis failed\. Please retry\./i)
      ).toBeInTheDocument();
      expect(
        screen.getByRole('button', { name: /retry analysis/i })
      ).toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });

  it('does NOT render resume / cover-letter sections when no target is selected', () => {
    // Tailor pipeline needs ``target_id`` — without one the section's
    // Generate button would 422. Hide the section cleanly instead.
    render(
      <JobDetailPanel
        posting={makeJob({ status: 'resume_draft' })}
        targetId={undefined}
        viewFullHref={undefined}
        onDelete={undefined}
        onStatusChange={undefined}
      />
    );
    expect(
      screen.queryByRole('link', { name: /review tailored resume/i })
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId('cover-letter-section-stub')
    ).not.toBeInTheDocument();
  });

  it('fetches status history on mount and renders entries when present', async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        entries: [
          {
            id: 'h-1',
            old_status: 'new',
            new_status: 'saved',
            note: null,
            created_at: '2026-01-02T00:00:00Z',
          },
        ],
      }),
    }) as unknown as typeof fetch;

    render(
      <JobDetailPanel
        posting={makeJob()}
        targetId={undefined}
        viewFullHref={undefined}
        onDelete={undefined}
        onStatusChange={undefined}
      />
    );

    await waitFor(() => {
      expect(screen.getByText(/history/i)).toBeInTheDocument();
    });
  });
});
