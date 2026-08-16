import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ResumeReviewPage from '../ResumeReviewPage';
import { hasRestorableMarkdown } from '../../../types';
import type { ResumeVersion } from '../../../types';

/**
 * Version restore used to fail for EVERY version: the API's `ResumeVersion`
 * model had no `payload_md` field and `extra: "ignore"`, so the column was
 * dropped on the way out and the page refused each row with "This version
 * predates markdown — cannot restore" — including one generated seconds
 * earlier. With the field restored, a version carrying markdown must offer a
 * working Load, and only genuinely markdown-less rows may refuse.
 */

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
  payload: { summary: 'live' },
  payload_md: '# Live draft',
  docx_payload_md_hash: null,
  storage_path: null,
  warnings: [],
  model: 'm',
  input_tokens: 1,
  output_tokens: 1,
  cost_usd: 0.01,
  latency_ms: 1,
  created_at: '2026-08-16T01:00:00Z',
  approved_at: null,
};

function version(over: Partial<ResumeVersion> = {}): ResumeVersion {
  return {
    id: 'v-1',
    resume_id: 'r-1',
    payload: { summary: 'snapshot' } as ResumeVersion['payload'],
    payload_md: '# Snapshot markdown',
    source: 'initial',
    created_at: '2026-08-16T01:06:30Z',
    ...over,
  };
}

function mockFetch(versions: ResumeVersion[]) {
  global.fetch = jest.fn().mockImplementation((url: string) => {
    if (url === '/api/jobs/j-1') {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => POSTING,
      });
    }
    if (url.includes('/tailor/by-job/')) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({ record: RECORD, status: 'idle' }),
      });
    }
    if (url.includes('/versions')) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({ versions, cap: 5 }),
      });
    }
    return Promise.resolve({ ok: false, status: 404, json: async () => ({}) });
  }) as unknown as typeof fetch;
}

const originalFetch = global.fetch;
beforeEach(() => mockToast.mockReset());
afterEach(() => {
  global.fetch = originalFetch;
});

describe('hasRestorableMarkdown', () => {
  it('accepts a snapshot carrying markdown', () => {
    expect(hasRestorableMarkdown(version())).toBe(true);
  });

  it('rejects null and empty markdown', () => {
    expect(hasRestorableMarkdown(version({ payload_md: null }))).toBe(false);
    expect(hasRestorableMarkdown(version({ payload_md: '' }))).toBe(false);
  });
});

describe('ResumeReviewPage version history', () => {
  async function openHistory() {
    const user = userEvent.setup();
    render(<ResumeReviewPage jobPostingId='j-1' />);
    const toggle = await screen.findByRole('button', {
      name: /version history/i,
    });
    await user.click(toggle);
    return user;
  }

  it('offers Load for a version that carries markdown', async () => {
    mockFetch([version()]);
    await openHistory();

    await waitFor(() =>
      expect(
        screen.getByRole('button', { name: /Load initial version/i })
      ).toBeInTheDocument()
    );
    expect(screen.queryByText(/Not restorable/i)).not.toBeInTheDocument();
  });

  it('marks a markdown-less version as not restorable instead of a dead-end button', async () => {
    mockFetch([version({ payload_md: null })]);
    await openHistory();

    await waitFor(() =>
      expect(screen.getByText(/Not restorable/i)).toBeInTheDocument()
    );
    expect(
      screen.queryByRole('button', { name: /Load initial version/i })
    ).not.toBeInTheDocument();
  });

  it('loading a version opens the confirm rather than erroring out', async () => {
    mockFetch([version()]);
    const user = await openHistory();

    const load = await screen.findByRole('button', {
      name: /Load initial version/i,
    });
    await user.click(load);

    // The old behaviour was an immediate error toast and no dialog.
    expect(mockToast).not.toHaveBeenCalledWith(
      expect.objectContaining({ variant: 'error' })
    );
  });
});
