import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import CoverLetterReviewPage from '../CoverLetterReviewPage';

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
  id: 'cl-1',
  user_id: 'u',
  job_posting_id: 'j-1',
  document_type: 'cover_letter' as const,
  resume_type: 'tech_friendly',
  jd_snapshot: 'snapshot',
  jd_snapshot_hash: 'h',
  payload: {} as unknown,
  payload_md: '# Cover letter draft',
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

describe('CoverLetterReviewPage', () => {
  it('renders a not-found state when the cover letter does not exist', async () => {
    global.fetch = jest.fn().mockResolvedValue({
      status: 404,
      ok: false,
      json: async () => ({}),
    } as Response) as unknown as typeof fetch;

    render(<CoverLetterReviewPage jobPostingId='j-1' />);

    expect(
      await screen.findByText(/Cover letter not found/i)
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
      if (url.includes('/cover-letter')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          // #656 envelope — the route returns {record, status}, NOT a bare
          // record. Mocking the bare shape is why this spec kept passing
          // while the page was actually inert in production.
          json: async () => ({ record: RECORD, status: 'idle' }),
        } as Response);
      }
      return Promise.resolve({
        ok: false,
        status: 404,
        json: async () => ({}),
      } as Response);
    }) as unknown as typeof fetch;

    render(<CoverLetterReviewPage jobPostingId='j-1' />);

    // Once the fetch resolves, the heading & editor toolbar appear.
    await waitFor(() => {
      // The full UI uses many headings; focusing on the loaded markdown
      // content as a unique anchor for "successful load".
      expect(screen.queryByLabelText(/Loading cover letter/i)).toBeNull();
    });
  });

  it('toasts an error when the network call rejects', async () => {
    global.fetch = jest
      .fn()
      .mockRejectedValue(new Error('boom')) as unknown as typeof fetch;

    render(<CoverLetterReviewPage jobPostingId='j-1' />);

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
 * Regression + parity for #656's cover-letter flagged drafts.
 *
 * The first test here is the one that was missing: the by-job cover-letter
 * route returns a `{record, status}` envelope, and this page was reading it as
 * a bare record — so `record.id` was undefined and the editor loaded empty.
 * It shipped because the spec above mocked the bare shape, which the real
 * route never returns. Reading `payload_md` through the envelope is what pins
 * it: against the old code the editor renders empty and this fails.
 */
describe('CoverLetterReviewPage — #656 envelope + flagged drafts', () => {
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
        if (url.includes('/cover-letter')) {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: async () => state,
          } as Response);
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({ entries: [], versions: [], cap: 5 }),
        } as Response);
      }) as unknown as typeof fetch;
  }

  it('reads the document out of the envelope, not the envelope itself', async () => {
    mockPage({ record: RECORD, status: 'idle' });
    render(<CoverLetterReviewPage jobPostingId='j-1' />);
    const surface = await screen.findByLabelText('Cover letter markdown');
    // The fixture's markdown — proves the record was read THROUGH the
    // envelope; the old code produced an empty editor here.
    expect(surface.textContent).toContain('Cover letter draft');
  });

  it('shows the flagged banner and a free re-check for a failing letter', async () => {
    mockPage({
      record: {
        ...RECORD,
        lint_violations: [
          {
            code: 'no_tables',
            message: 'Markdown contains a table.',
            severity: 'error',
          },
        ],
      },
      status: 'idle',
    });
    render(<CoverLetterReviewPage jobPostingId='j-1' />);
    expect(await screen.findByText(/Failed ATS checks/i)).toBeInTheDocument();
    expect(screen.getByText(/Markdown contains a table/i)).toBeInTheDocument();
    expect(screen.getByText(/no AI credits/i)).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /re-run ATS checks/i })
    ).toBeInTheDocument();
  });

  it('treats a clean letter as unflagged', async () => {
    mockPage({ record: { ...RECORD, lint_violations: [] }, status: 'idle' });
    render(<CoverLetterReviewPage jobPostingId='j-1' />);
    await screen.findByLabelText('Cover letter markdown');
    expect(screen.queryByText(/Failed ATS checks/i)).toBeNull();
  });
});
