import React from 'react';
import '@testing-library/jest-dom';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import ResumeReviewPage from '../ResumeReviewPage';

const mockToast = jest.fn();
jest.mock('@/state/Toast/ToastProvider', () => ({
  useToast: () => ({ toast: mockToast }),
}));

jest.mock('next/navigation', () => ({
  useRouter: () => ({ push: jest.fn(), prefetch: jest.fn() }),
}));

const POSTING = {
  id: 'j-1',
  external_id: 'ext',
  source_id: 'src',
  title: 'Senior FE',
  company_name: 'Acme',
  location: null,
  absolute_url: null,
  score: 80,
  score_breakdown: null,
  scoring_status: 'complete' as const,
  status: 'new',
  salary_text: null,
  source_posted_at: null,

  cataloged_at: '2026-04-30T00:00:00Z',
};

const RECORD = {
  id: 'r-1',
  user_id: 'u',
  job_posting_id: 'j-1',
  document_type: 'resume' as const,
  resume_type: 'tech_friendly',
  jd_snapshot: 'snapshot',
  jd_snapshot_hash: 'h',
  payload: {} as unknown,
  payload_md: '# Resume markdown',
  docx_payload_md_hash: null,
  storage_path: null,
  warnings: [],
  model: null,
  input_tokens: 0,
  output_tokens: 0,
  cost_usd: 0,
  latency_ms: 0,
  created_at: '2026-04-30T00:00:00Z',
  updated_at: null,
  approved_at: null,
  source_resume_id: null,
};

const originalFetch = global.fetch;

beforeEach(() => {
  mockToast.mockReset();
});

afterEach(() => {
  global.fetch = originalFetch;
});

describe('ResumeReviewPage', () => {
  it('renders a not-found state when the resume does not exist', async () => {
    global.fetch = jest.fn().mockResolvedValue({
      status: 404,
      ok: false,
      json: async () => ({}),
    } as Response) as unknown as typeof fetch;

    render(<ResumeReviewPage jobPostingId='j-1' />);

    expect(
      await screen.findByText(/Tailored resume not found/i)
    ).toBeInTheDocument();
    expect(
      screen.getByRole('link', { name: /Back to job/i })
    ).toBeInTheDocument();
  });

  it('renders the editor once both fetches succeed', async () => {
    global.fetch = jest.fn().mockImplementation((url: string) => {
      if (url === '/api/jobs/j-1') {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => POSTING,
        } as Response);
      }
      if (url.includes('/tailor/by-job/')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          // #656: the by-job route returns a {record, status} envelope.
          json: async () => ({ record: RECORD, status: 'idle' }),
        } as Response);
      }
      return Promise.resolve({
        ok: false,
        status: 404,
        json: async () => ({}),
      } as Response);
    }) as unknown as typeof fetch;

    render(<ResumeReviewPage jobPostingId='j-1' />);

    await waitFor(() => {
      expect(screen.queryByLabelText(/Loading resume/i)).toBeNull();
    });

    // The TipTap-backed editor surface exposes the same aria-label the
    // old textarea did, so existing flow tests keep working. Smoke-check
    // it actually rendered the loaded markdown (vs being a hollow div).
    const surface = await screen.findByLabelText('Resume markdown');
    expect(surface.getAttribute('contenteditable')).toBe('true');
    expect(surface.textContent).toContain('Resume markdown');

    // Filename input is present with a slug-derived placeholder. The
    // placeholder is exposed via getByPlaceholderText since the input
    // value starts empty until the user types.
    const filenameInput = screen.getByLabelText(
      'Download filename'
    ) as HTMLInputElement;
    expect(filenameInput).toBeInTheDocument();
    expect(filenameInput.placeholder).toMatch(/-acme-/);
    expect(filenameInput.value).toBe('');
  });

  it('toasts an error when the network call rejects', async () => {
    global.fetch = jest
      .fn()
      .mockRejectedValue(new Error('boom')) as unknown as typeof fetch;

    render(<ResumeReviewPage jobPostingId='j-1' />);

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          variant: 'error',
          title: expect.stringMatching(/Network error/i),
        })
      );
    });
  });
});

/**
 * Flagged drafts (#656). A resume that fails ATS lint is now PERSISTED with
 * its violations rather than 422'd away — the LLM call is already paid for and
 * regenerating burns the daily cap. These pin the half of that decision the
 * user actually sees: the draft opens, says what's wrong, and offers the free
 * deterministic re-check instead of a paid regeneration.
 */
const FLAGGED_VIOLATION = {
  code: 'no_tables',
  message: 'Markdown contains a table.',
  severity: 'error' as const,
};

function mockPage(
  state: unknown,
  extra?: (url: string, init?: { method?: string }) => unknown
) {
  global.fetch = jest
    .fn()
    .mockImplementation((url: string, init?: { method?: string }) => {
      const custom = extra?.(url, init);
      if (custom) return Promise.resolve(custom as Response);
      if (url === '/api/jobs/j-1') {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => POSTING,
        } as Response);
      }
      if (url.includes('/tailor/by-job/')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => state,
        } as Response);
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({ entries: [] }),
      } as Response);
    }) as unknown as typeof fetch;
}

describe('ResumeReviewPage — flagged drafts (#656)', () => {
  it('shows why the draft failed ATS checks, with a free re-check action', async () => {
    mockPage({
      record: { ...RECORD, lint_violations: [FLAGGED_VIOLATION] },
      status: 'idle',
    });

    render(<ResumeReviewPage jobPostingId='j-1' />);

    expect(await screen.findByText(/Failed ATS checks/i)).toBeInTheDocument();
    expect(screen.getByText(/Markdown contains a table/i)).toBeInTheDocument();
    expect(screen.getByText(/Needs fixes/i)).toBeInTheDocument();
    // The banner names the cost, because "regenerate" is the expensive
    // alternative the flagged-persist decision exists to avoid.
    expect(screen.getByText(/no AI credits/i)).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /re-run ATS checks/i })
    ).toBeInTheDocument();
  });

  it('clears the flag when a re-check passes', async () => {
    const posted: string[] = [];
    mockPage(
      {
        record: { ...RECORD, lint_violations: [FLAGGED_VIOLATION] },
        status: 'idle',
      },
      (url, init) => {
        if (init?.method === 'POST' && url.includes('/ats-recheck')) {
          posted.push(url);
          return {
            ok: true,
            status: 200,
            json: async () => ({
              ok: true,
              violations: [],
              record: { ...RECORD, lint_violations: [] },
            }),
          };
        }
        return undefined;
      }
    );

    render(<ResumeReviewPage jobPostingId='j-1' />);
    const button = await screen.findByRole('button', {
      name: /re-run ATS checks/i,
    });

    fireEvent.click(button);

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          variant: 'success',
          title: 'Passes ATS checks',
        })
      );
    });
    expect(posted).toEqual(['/api/jobs/tailor/r-1/ats-recheck']);
    // Banner gone: `lint_violations: []` is "linted clean", not "unlinted".
    await waitFor(() => {
      expect(screen.queryByText(/Failed ATS checks/i)).toBeNull();
    });
  });

  it('renders a wait, not a dead end, when landing mid-generation', async () => {
    // Kicked off from the job panel, then navigated straight here. The run
    // outlives that navigation, so "not found" would be a lie.
    mockPage({ record: null, status: 'running' });

    render(<ResumeReviewPage jobPostingId='j-1' />);

    expect(
      await screen.findByText(/Tailoring your resume/i)
    ).toBeInTheDocument();
    expect(
      screen.getByText(/keeps running if you navigate away/i)
    ).toBeInTheDocument();
    expect(screen.queryByText(/Tailored resume not found/i)).toBeNull();
  });

  it('still reports a genuinely missing draft as not found', async () => {
    // The negative that keeps the wait state honest: a 200 + null record with
    // nothing in flight is a settled empty state, not a pending one.
    mockPage({ record: null, status: 'idle' });

    render(<ResumeReviewPage jobPostingId='j-1' />);

    expect(
      await screen.findByText(/Tailored resume not found/i)
    ).toBeInTheDocument();
  });

  describe('posting 404 — pre-scoring window (release smoke 2026-08-13)', () => {
    // ``GET /jobs/{id}`` gates on a ``scores`` row, so a just-added manual
    // posting 404s until background scoring lands — and the onboarding
    // path-A payoff navigates here inside exactly that window. The posting
    // fetch must degrade the chrome, never gate the user's own document.
    const posting404 = (url: string) =>
      url === '/api/jobs/j-1'
        ? { ok: false, status: 404, json: async () => ({}) }
        : undefined;

    it('renders the draft even while the posting is still unscored', async () => {
      mockPage({ record: RECORD, status: 'idle' }, posting404);

      render(<ResumeReviewPage jobPostingId='j-1' />);

      const surface = await screen.findByLabelText('Resume markdown');
      expect(surface.textContent).toContain('Resume markdown');
      expect(screen.queryByText(/Tailored resume not found/i)).toBeNull();
      // The subtitle says why the job header is missing instead of
      // pretending nothing exists.
      expect(
        screen.getByText(/Job details are still processing/i)
      ).toBeInTheDocument();
      // Filename degrades to name-date — there is no company to slug.
      const filenameInput = screen.getByLabelText(
        'Download filename'
      ) as HTMLInputElement;
      expect(filenameInput.placeholder).toMatch(/^resume-/);
      expect(filenameInput.placeholder).not.toMatch(/acme/i);
    });

    it('shows the wait state, not "not found", when landing mid-generation', async () => {
      // The payoff's actual arrival shape: tailor 202 accepted, run in
      // flight, posting unscored. The old gate slammed this to "not found".
      mockPage({ record: null, status: 'running' }, posting404);

      render(<ResumeReviewPage jobPostingId='j-1' />);

      expect(
        await screen.findByText(/Tailoring your resume/i)
      ).toBeInTheDocument();
      expect(screen.queryByText(/Tailored resume not found/i)).toBeNull();
    });

    it('still reports not-found when there is no document either', async () => {
      // A garbage/foreign posting id has no document and nothing running —
      // the honest dead end survives the gate change.
      mockPage({ record: null, status: 'idle' }, posting404);

      render(<ResumeReviewPage jobPostingId='j-1' />);

      expect(
        await screen.findByText(/Tailored resume not found/i)
      ).toBeInTheDocument();
    });
  });
});
