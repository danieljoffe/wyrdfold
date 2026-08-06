import React from 'react';
import '@testing-library/jest-dom';
import { act, fireEvent, render, screen } from '@testing-library/react';
import ResumeReviewPage from '../ResumeReviewPage';

/**
 * The approve-vs-autosave race (observed live 2026-08-06, twice): an edit
 * or version-restore that lands around the approve flight arms the 1.5s
 * autosave debounce, and its timer used to outlive the lock — PATCH →
 * 409 "already approved" + an error toast right after the green "Resume
 * locked" one. The invariant under test: ``persistMarkdown`` never
 * PATCHes a record whose ``approved_at`` is set, whatever armed it.
 */

const mockToast = jest.fn();
jest.mock('@/state/Toast/ToastProvider', () => ({
  useToast: () => ({ toast: mockToast }),
}));

jest.mock('next/navigation', () => ({
  useRouter: () => ({ push: jest.fn(), prefetch: jest.fn() }),
}));

// Stub the TipTap-backed editor with a button that fires ``onChange`` —
// the deterministic stand-in for "an edit armed the debounce". The stub
// deliberately ignores ``disabled``: the real race arms the state via
// paths the disabled editor can't block (restore finishing mid-approve).
jest.mock('@/components/MarkdownPreviewEditor', () => ({
  __esModule: true,
  default: ({
    onChange,
    value,
  }: {
    onChange: (next: string) => void;
    value: string;
  }) => (
    <button
      data-testid='editor-stub-edit'
      onClick={() => onChange(`${value} edited`)}
    >
      edit
    </button>
  ),
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

const APPROVED_RECORD = {
  id: 'r-1',
  user_id: 'u',
  job_posting_id: 'j-1',
  document_type: 'resume' as const,
  resume_type: 'tech_friendly',
  jd_snapshot: 'snapshot',
  jd_snapshot_hash: 'h',
  payload: {} as unknown,
  payload_md: '# Locked resume',
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
  approved_at: '2026-08-06T01:35:38Z',
  source_resume_id: null,
};

const originalFetch = global.fetch;

afterEach(() => {
  global.fetch = originalFetch;
  jest.useRealTimers();
  jest.clearAllMocks();
});

test('an autosave armed against a locked record never PATCHes (no 409, no error toast)', async () => {
  const patchCalls: string[] = [];
  global.fetch = jest.fn((url: string, init?: { method?: string }) => {
    if (init?.method === 'PATCH') {
      patchCalls.push(url);
      return Promise.resolve({
        ok: false,
        status: 409,
        json: async () => ({
          detail: 'document already approved — cannot edit',
        }),
      });
    }
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
        json: async () => APPROVED_RECORD,
      });
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      json: async () => ({ entries: [] }),
    });
  }) as unknown as typeof fetch;

  render(<ResumeReviewPage jobPostingId='j-1' />);
  const editButton = await screen.findByTestId('editor-stub-edit');

  // Arm the debounce against the locked record (stands in for the
  // restore/edit that finishes while the approve flight is landing).
  jest.useFakeTimers();
  fireEvent.click(editButton);

  // Let the 1.5s debounce fire and flush its microtasks.
  await act(async () => {
    jest.advanceTimersByTime(3_000);
  });

  expect(patchCalls).toHaveLength(0);
  expect(mockToast).not.toHaveBeenCalledWith(
    expect.objectContaining({ variant: 'error' })
  );
});
