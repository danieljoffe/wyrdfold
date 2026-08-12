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

/**
 * #650: the pending-row breakdown showed RAW keyword points under a headline
 * in a different unit, hid zero components, and scaled bars to the largest
 * entry — so "+80" and "+4" sat under a 60 with no stated relationship.
 *
 * Two unit changes separate those numbers:
 *   components (raw points) → fit% (÷ the target's max) → × freshness
 * The panel now shows the whole chain so it reconciles to the card's number.
 */
test('pending row reconciles components → fit → freshness → the score shown', () => {
  stubFetch({ ok: false });

  // The issue's own row: raw components 80 + 4, fit 60, aged so the card
  // shows 39 (60 × 0.65). Before this, the panel showed "+80"/"+4" and 60.
  renderPanel(
    makeJob({
      scoring_status: 'stage1',
      pending: true,
      axis_scores: null,
      score: 39,
      raw_score: 60,
      score_breakdown: {
        role_titles: 80,
        technologies: 4,
        domain_skills: 0,
        seniority_signals: 0,
        negative: 0,
      },
      source_posted_at: new Date(Date.now() - 30 * 86_400_000).toISOString(),
    })
  );

  // THE assertion: components are apportioned into the fit score, so they
  // SUM to it. 80 and 4 raw → 80/84×60 = 57.1 and 4/84×60 = 2.9.
  // Asserting the summary rows alone is vacuous — reverting to raw points
  // still renders "Fit 60 / ×0.65 / 39" and would pass. Caught by sabotage.
  expect(screen.getByText('57.1')).toBeInTheDocument();
  expect(screen.getByText('2.9')).toBeInTheDocument();
  expect(screen.queryByText('80')).toBeNull(); // the raw point value is gone
  expect(screen.getByText('Fit against this target')).toBeInTheDocument();

  // Freshness is shown as its own step. Derived from displayed ÷ fit, so the
  // decay formula is never duplicated client-side.
  expect(screen.getByText(/Freshness/)).toBeInTheDocument();
  expect(screen.getByText('×0.65')).toBeInTheDocument();

  // …and the chain lands on the number the card actually shows.
  // The headline chip renders 39 too, so scope to the chain's own row rather
  // than a bare text match — otherwise this passes on the chip alone and
  // asserts nothing about the reconciliation.
  const shownRow = screen.getByText('Score shown').closest('div');
  expect(shownRow).toHaveTextContent('39');
});

test('pending row shows zero components instead of hiding them', () => {
  stubFetch({ ok: false });

  renderPanel(
    makeJob({
      scoring_status: 'stage1',
      pending: true,
      axis_scores: null,
      score: 60,
      raw_score: 60,
      score_breakdown: {
        role_titles: 80,
        technologies: 4,
        domain_skills: 0,
        seniority_signals: 0,
      },
    })
  );

  // "domain skills scored nothing" is arguably the most actionable signal on
  // the card; the old list filtered `v !== 0` and dropped it silently.
  expect(screen.getByText('Domain skills')).toBeInTheDocument();
  expect(screen.getByText('Seniority signals')).toBeInTheDocument();
});

test('pending row without raw_score degrades to raw points, no invented chain', () => {
  stubFetch({ ok: false });

  // Responses predating #665's projection omit raw_score. The panel must not
  // fabricate a fit/freshness split it cannot derive.
  renderPanel(
    makeJob({
      scoring_status: 'stage1',
      pending: true,
      axis_scores: null,
      score: 60,
      score_breakdown: { role_titles: 80, technologies: 4 },
    })
  );

  expect(screen.getByText('Role titles')).toBeInTheDocument();
  expect(screen.queryByText('Fit against this target')).toBeNull();
  expect(screen.queryByText(/Freshness/)).toBeNull();
});
