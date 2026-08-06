import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import JobDetailPanel from '../JobDetailPanel';
import type { JobPosting } from '../types';

/**
 * #609 follow-up: the Score Breakdown section must explain the number next
 * to it. Graded rows carry an axis-blend score, so they render the fit AXES
 * (whose average IS the score); pending rows keep the keyword components
 * (their score IS the keyword sum). Rows served by the RPC list paths ship
 * no ``axis_scores`` key at all — the panel lazily pulls the detail GET,
 * and falls back to keyword components if that fetch fails.
 */

const mockToast = jest.fn();
jest.mock('@/state/Toast/ToastProvider', () => ({
  useToast: () => ({ toast: mockToast }),
}));

jest.mock('next/navigation', () => ({
  useRouter: () => ({ push: jest.fn(), prefetch: jest.fn() }),
}));

// The tailor/feedback sections each own their fetch flows — stub them so
// this spec exercises only the breakdown branch.
jest.mock('../ResumeSection', () => ({
  __esModule: true,
  default: () => <div data-testid='resume-section-stub' />,
}));
jest.mock('../CoverLetterSection', () => ({
  __esModule: true,
  default: () => <div data-testid='cover-letter-section-stub' />,
}));
jest.mock('../JobFeedbackSection', () => ({
  __esModule: true,
  default: () => <div data-testid='feedback-section-stub' />,
}));

const ORIGINAL_FETCH = global.fetch;

/** Routing fetch stub: the panel's other sections (status history, delete)
 * fetch too — give them a benign OK so the tree stays mounted, and let each
 * test override only the detail-GET route it cares about. */
function stubFetch(detailResponse?: {
  ok: boolean;
  json?: () => Promise<unknown>;
}) {
  const spy = jest.fn((url: string) => {
    if (url === '/api/jobs/j-1' && detailResponse) {
      return Promise.resolve(detailResponse);
    }
    // Shape matters: status-history reads ``{ entries: [] }`` off this.
    return Promise.resolve({ ok: true, json: async () => ({ entries: [] }) });
  });
  global.fetch = spy as unknown as typeof fetch;
  return spy;
}

const AXES = {
  title_fit: 95,
  skills_fit: 85,
  seniority_fit: 90,
  domain_fit: 95,
};

function makeJob(overrides: Partial<JobPosting> = {}): JobPosting {
  return {
    id: 'j-1',
    external_id: 'ext-1',
    source_id: 'src-1',
    title: 'Staff Full Stack Software Engineer',
    company_name: 'Bostondynamics',
    location: 'Waltham, MA, US',
    absolute_url: null,
    score: 91,
    score_breakdown: { role_titles: 24.4, technologies: 20.1 },
    scoring_status: 'complete',
    pending: false,
    status: 'new',
    salary_text: null,
    source_posted_at: null,
    cataloged_at: '2026-08-01T00:00:00Z',
    ...overrides,
  };
}

function renderPanel(posting: JobPosting) {
  return render(
    <JobDetailPanel
      posting={posting}
      targetId={undefined}
      viewFullHref={`/jobs/${posting.id}`}
      onDelete={undefined}
      onStatusChange={undefined}
    />
  );
}

afterEach(() => {
  global.fetch = ORIGINAL_FETCH;
  jest.clearAllMocks();
});

test('graded row with axes from the list payload renders fit axes, not keyword components', () => {
  const fetchSpy = stubFetch();

  renderPanel(makeJob({ axis_scores: AXES }));

  expect(screen.getByText('Title fit')).toBeInTheDocument();
  expect(screen.getByText('Skills fit')).toBeInTheDocument();
  expect(screen.getByText('Seniority fit')).toBeInTheDocument();
  expect(screen.getByText('Domain fit')).toBeInTheDocument();
  expect(screen.queryByText('Role titles')).not.toBeInTheDocument();
  expect(screen.queryByText('Technologies')).not.toBeInTheDocument();
  // Axes were on the payload — no detail-GET fallback fired.
  expect(fetchSpy).not.toHaveBeenCalledWith('/api/jobs/j-1');
});

test('pending row keeps the keyword components and never fetches the detail', () => {
  const fetchSpy = stubFetch();

  renderPanel(
    makeJob({
      score: 42,
      pending: true,
      scoring_status: 'stage2',
      axis_scores: null,
      score_breakdown: { role_titles: 30.0, technologies: 12.2 },
    })
  );

  expect(screen.getByText('Role titles')).toBeInTheDocument();
  expect(screen.queryByText('Title fit')).not.toBeInTheDocument();
  expect(fetchSpy).not.toHaveBeenCalledWith('/api/jobs/j-1');
});

test('graded row WITHOUT the axis_scores key lazily fetches the detail GET and swaps to axes', async () => {
  const fetchSpy = stubFetch({
    ok: true,
    json: async () => ({ axis_scores: AXES }),
  });

  const job = makeJob();
  delete (job as Partial<JobPosting>).axis_scores; // RPC-served row shape
  renderPanel(job);

  await waitFor(() =>
    expect(screen.getByText('Title fit')).toBeInTheDocument()
  );
  expect(fetchSpy).toHaveBeenCalledWith('/api/jobs/j-1');
  expect(screen.queryByText('Role titles')).not.toBeInTheDocument();
});

test('graded row falls back to keyword components when the detail fetch fails', async () => {
  stubFetch({ ok: false });

  const job = makeJob();
  delete (job as Partial<JobPosting>).axis_scores;
  renderPanel(job);

  await waitFor(() =>
    expect(screen.getByText('Role titles')).toBeInTheDocument()
  );
  expect(screen.queryByText('Title fit')).not.toBeInTheDocument();
});
