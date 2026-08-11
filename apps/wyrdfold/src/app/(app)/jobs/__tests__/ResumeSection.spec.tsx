import React from 'react';
import '@testing-library/jest-dom';
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import ResumeSection from '../ResumeSection';
import type { TailoredResumeRecord } from '../types';

/**
 * The client half of non-blocking tailoring (#656).
 *
 * The behaviors that only exist because generation is backgrounded — and that
 * a blocking implementation would fail: adopting a run that started before
 * this component mounted (the "survives navigation" requirement), handling a
 * 202 by polling rather than reading a record off the response, and labelling
 * a flagged draft as something to fix rather than review.
 */

const mockToast = jest.fn();
jest.mock('@/state/Toast/ToastProvider', () => ({
  useToast: () => ({ toast: mockToast }),
}));

jest.mock('next/navigation', () => ({
  useRouter: () => ({ push: jest.fn(), prefetch: jest.fn() }),
}));

const ORIGINAL_FETCH = global.fetch;

function makeRecord(
  overrides: Partial<TailoredResumeRecord> = {}
): TailoredResumeRecord {
  return {
    id: 'r-1',
    user_id: 'u-1',
    job_posting_id: 'j-1',
    document_type: 'resume',
    resume_type: 'generic',
    jd_snapshot: 'JD',
    jd_snapshot_hash: 'h',
    payload: {} as never,
    payload_md: '# Resume',
    docx_payload_md_hash: null,
    storage_path: null,
    warnings: [],
    model: null,
    input_tokens: 0,
    output_tokens: 0,
    cost_usd: 0,
    latency_ms: 0,
    created_at: '2026-08-07T00:00:00Z',
    updated_at: null,
    approved_at: null,
    source_resume_id: null,
    ...overrides,
  };
}

/** Advance past one poll interval and flush the fetch microtasks it triggers. */
async function tickPoll() {
  await act(async () => {
    jest.advanceTimersByTime(2600);
  });
}

beforeEach(() => {
  jest.clearAllMocks();
});

afterEach(() => {
  jest.useRealTimers();
  global.fetch = ORIGINAL_FETCH;
});

describe('ResumeSection — background generation (#656)', () => {
  it('adopts a run already in flight on mount, without kicking a second one', async () => {
    // The "survives navigation" case: generation was started from another
    // view (or another tab), the user lands here mid-run. Re-POSTing would
    // spend a second time; showing "Generate" would lie about the state.
    const calls: { url: string; method?: string | undefined }[] = [];
    let polls = 0;
    global.fetch = jest
      .fn()
      .mockImplementation((url: string, init?: { method?: string }) => {
        calls.push({ url, method: init?.method });
        polls += 1;
        return Promise.resolve({
          ok: true,
          status: 200,
          // First read: running with no record yet. Then the record lands.
          json: async () =>
            polls === 1
              ? { record: null, status: 'running' }
              : { record: makeRecord(), status: 'idle' },
        });
      }) as unknown as typeof fetch;

    jest.useFakeTimers();
    render(<ResumeSection jobPostingId='j-1' />);

    await waitFor(() => {
      expect(
        screen.getByRole('button', { name: /generating/i })
      ).toBeDisabled();
    });
    // No POST was fired — the run was adopted, not restarted.
    expect(calls.every(c => c.method !== 'POST')).toBe(true);

    await tickPoll();

    const link = await screen.findByRole('link', {
      name: /review tailored resume/i,
    });
    expect(link).toHaveAttribute('href', '/jobs/j-1/resume');
    expect(calls.filter(c => c.method === 'POST')).toHaveLength(0);
  });

  /**
   * A fetch mock whose poll answers change only AFTER the kick-off POST, so
   * the mount read (which must show the Generate CTA) can't be confused with
   * the post-202 polling.
   */
  function mockKickThenPolls(afterKick: unknown[]) {
    const posts: string[] = [];
    let served = 0;
    global.fetch = jest
      .fn()
      .mockImplementation((url: string, init?: { method?: string }) => {
        if (url === '/api/jobs/j-1') {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: async () => ({ description_html: '<p>Build things</p>' }),
          });
        }
        if (init?.method === 'POST') {
          posts.push(url);
          // The whole point: 202, not 200-with-a-record.
          return Promise.resolve({
            ok: true,
            status: 202,
            json: async () => ({ status: 'running' }),
          });
        }
        if (posts.length === 0) {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: async () => ({ record: null, status: 'idle' }),
          });
        }
        const body = afterKick[Math.min(served, afterKick.length - 1)];
        served += 1;
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => body,
        });
      }) as unknown as typeof fetch;
    return posts;
  }

  it('kicks off, gets a 202, and polls until the document lands', async () => {
    mockKickThenPolls([
      { record: null, status: 'running' },
      { record: makeRecord(), status: 'idle' },
    ]);

    render(<ResumeSection jobPostingId='j-1' />);
    const generate = await screen.findByRole('button', {
      name: /generate tailored resume/i,
    });

    jest.useFakeTimers();
    await act(async () => {
      fireEvent.click(generate);
    });

    expect(screen.getByRole('button', { name: /generating/i })).toBeDisabled();

    await tickPoll(); // still running
    await tickPoll(); // record lands

    expect(
      await screen.findByRole('link', { name: /review tailored resume/i })
    ).toBeInTheDocument();
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({ variant: 'success' })
    );
  });

  it('surfaces a background failure instead of spinning forever', async () => {
    mockKickThenPolls([
      {
        record: null,
        status: 'error',
        message: 'Resume generation failed. Please retry.',
      },
    ]);

    render(<ResumeSection jobPostingId='j-1' />);
    const generate = await screen.findByRole('button', {
      name: /generate tailored resume/i,
    });

    jest.useFakeTimers();
    await act(async () => {
      fireEvent.click(generate);
    });
    await tickPoll();

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          variant: 'error',
          title: 'Resume generation failed. Please retry.',
        })
      );
    });
    // ...and the CTA comes back so the user can retry.
    expect(
      screen.getByRole('button', { name: /generate tailored resume/i })
    ).toBeInTheDocument();
  });

  it('labels a flagged draft as something to fix, not review', async () => {
    // The flagged-persist decision only pays off if the user can tell the
    // draft needs attention before opening it.
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        record: makeRecord({
          lint_violations: [
            {
              code: 'no_tables',
              message: 'Contains a table',
              severity: 'error',
            },
          ],
        }),
        status: 'idle',
      }),
    }) as unknown as typeof fetch;

    render(<ResumeSection jobPostingId='j-1' />);

    expect(
      await screen.findByRole('link', { name: /fix tailored resume/i })
    ).toBeInTheDocument();
  });

  it('treats a warnings-only lint result as clean, not flagged', async () => {
    // The negative that gives the badge meaning: warnings are advisories, so
    // a length check on lint_violations would wrongly flag a healthy draft.
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        record: makeRecord({
          lint_violations: [
            { code: 'long_line', message: 'A bit long', severity: 'warning' },
          ],
        }),
        status: 'idle',
      }),
    }) as unknown as typeof fetch;

    render(<ResumeSection jobPostingId='j-1' />);

    expect(
      await screen.findByRole('link', { name: /review tailored resume/i })
    ).toBeInTheDocument();
  });

  it('renders the Generate CTA when nothing exists and nothing is running', async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ record: null, status: 'idle' }),
    }) as unknown as typeof fetch;

    render(<ResumeSection jobPostingId='j-1' />);

    expect(
      await screen.findByRole('button', { name: /generate tailored resume/i })
    ).toBeInTheDocument();
  });
});
