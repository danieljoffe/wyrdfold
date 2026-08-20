import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import CoverLetterSection from '../CoverLetterSection';
import type { TailoredResumeRecord } from '../types';

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
    id: 'cl-1',
    user_id: 'u-1',
    job_posting_id: 'j-1',
    document_type: 'cover_letter',
    resume_type: 'standard',
    jd_snapshot: '...',
    jd_snapshot_hash: 'hash',
    payload: {
      contact: {
        name: 'Daniel',
        email: null,
        phone: null,
        location: null,
        website: null,
        linkedin: null,
      },
      recipient_company: 'Acme',
      recipient_role: null,
      salutation: 'Hi',
      paragraphs: [],
      closing: 'Best',
      signature: 'Daniel',
      jd_snippet: '...',
      preferences_applied: [],
      source_outcome_refs: [],
      source_role_refs: [],
      source_skill_refs: [],
    },
    payload_md: null,
    docx_payload_md_hash: null,
    storage_path: null,
    warnings: [],
    model: null,
    input_tokens: 0,
    output_tokens: 0,
    cost_usd: 0,
    latency_ms: 0,
    created_at: '2026-01-01',
    updated_at: null,
    approved_at: null,
    source_resume_id: null,
    ...overrides,
  };
}

beforeEach(() => {
  jest.clearAllMocks();
});

afterAll(() => {
  global.fetch = ORIGINAL_FETCH;
});

describe('CoverLetterSection', () => {
  it('renders the empty/not-started state with a Generate CTA when no record exists', async () => {
    // ``/api/jobs/tailor/by-job/{id}/cover-letter`` now returns 200
    // with a ``null`` body when no record exists (was 404). The
    // section treats both null and the old 404 the same way —
    // render the Generate CTA — see the route docstring for the
    // browser-console-noise rationale.
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ record: null, status: 'idle' }),
    }) as unknown as typeof fetch;

    render(
      <CoverLetterSection
        jobPostingId='j-1'
        companyName='Acme'
        roleTitle='SWE'
      />
    );

    expect(
      await screen.findByRole('button', { name: /generate cover letter/i })
    ).toBeInTheDocument();
  });

  it('renders a Review link for an unapproved cover letter', async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ record: makeRecord(), status: 'idle' }),
    }) as unknown as typeof fetch;

    render(
      <CoverLetterSection
        jobPostingId='j-1'
        companyName='Acme'
        roleTitle='SWE'
      />
    );

    const reviewLink = await screen.findByRole('link', {
      name: /review cover letter/i,
    });
    expect(reviewLink).toHaveAttribute('href', '/jobs/j-1/cover-letter');
  });

  it('renders a View link when the cover letter is approved', async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        record: makeRecord({ approved_at: '2026-01-02' }),
        status: 'idle',
      }),
    }) as unknown as typeof fetch;

    render(
      <CoverLetterSection
        jobPostingId='j-1'
        companyName='Acme'
        roleTitle='SWE'
      />
    );

    const viewLink = await screen.findByRole('link', {
      name: /view cover letter/i,
    });
    expect(viewLink).toHaveAttribute('href', '/jobs/j-1/cover-letter');
  });

  it('toasts an error when the generation request fails (non-2xx)', async () => {
    const calls: string[] = [];
    global.fetch = jest.fn().mockImplementation((url: string) => {
      calls.push(url);
      // Initial fetch — 200 with null body (no existing record).
      // Switched from 404 in the by-job route to avoid the
      // browser auto-logging a console error on every job-detail
      // visit before generation.
      if (calls.length === 1) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({ record: null, status: 'idle' }),
        });
      }
      // Generate POST — 500
      return Promise.resolve({
        ok: false,
        status: 500,
        json: async () => ({}),
      });
    }) as unknown as typeof fetch;

    const user = userEvent.setup();
    render(
      <CoverLetterSection
        jobPostingId='j-1'
        companyName='Acme'
        roleTitle='SWE'
      />
    );

    await user.click(
      await screen.findByRole('button', { name: /generate cover letter/i })
    );

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: 'error' })
      );
    });
  });

  /**
   * The skip-confirm click IS the user's "I know it's a reach, write it
   * anyway" — the API's ``allow_stretch`` exists precisely so the model does
   * not decline on their behalf and bill them for the refusal. The flag was
   * added API-side first and would have shipped inert: nothing in the FE sent
   * it, and no test could tell.
   */
  function mockGenerateFlow(captured: { body?: Record<string, unknown> }) {
    let n = 0;
    global.fetch = jest
      .fn()
      .mockImplementation((url: string, init?: RequestInit) => {
        n += 1;
        if (n === 1) {
          // Initial poll — no existing record.
          return Promise.resolve({
            ok: true,
            status: 200,
            json: async () => ({ record: null, status: 'idle' }),
          });
        }
        if (String(url).includes('/api/jobs/j-1') && !init?.method) {
          // loadJobDescription
          return Promise.resolve({
            ok: true,
            status: 200,
            json: async () => ({ description_html: 'Senior UX Designer JD' }),
          });
        }
        captured.body = JSON.parse(String(init?.body ?? '{}'));
        return Promise.resolve({
          ok: true,
          status: 202,
          json: async () => ({}),
        });
      }) as unknown as typeof fetch;
  }

  it('sends allow_stretch when the user confirms past the skip warning', async () => {
    const captured: { body?: Record<string, unknown> } = {};
    mockGenerateFlow(captured);

    const user = userEvent.setup();
    render(
      <CoverLetterSection
        jobPostingId='j-1'
        companyName='Acme'
        roleTitle='Senior UX Designer'
        skipReason='Skip — no product design experience.'
      />
    );

    await user.click(
      await screen.findByRole('button', { name: /generate cover letter/i })
    );
    // The warning gates the spend; confirming is the explicit override.
    await user.click(
      await screen.findByRole('button', { name: /^generate anyway$/i })
    );

    await waitFor(() => expect(captured.body).toBeDefined());
    expect(captured.body).toMatchObject({ allow_stretch: true });
  });

  it('omits allow_stretch when the job carries no skip recommendation', async () => {
    const captured: { body?: Record<string, unknown> } = {};
    mockGenerateFlow(captured);

    const user = userEvent.setup();
    render(
      <CoverLetterSection
        jobPostingId='j-1'
        companyName='Acme'
        roleTitle='SWE'
      />
    );

    await user.click(
      await screen.findByRole('button', { name: /generate cover letter/i })
    );

    await waitFor(() => expect(captured.body).toBeDefined());
    // A good-fit job must keep the default prompt — the stretch block is an
    // opt-in, not a global loosening of the refusal behaviour.
    expect(captured.body).not.toHaveProperty('allow_stretch');
  });
});
