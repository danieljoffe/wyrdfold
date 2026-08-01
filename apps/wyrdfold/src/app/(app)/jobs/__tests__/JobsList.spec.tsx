import '@testing-library/jest-dom';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import JobsList, { type TargetTab } from '../JobsList';
import type { JobPosting, JobsFilterState } from '../types';

const mockReplace = jest.fn();
const mockToast = jest.fn();

// Live URL-state mock that triggers re-renders on write — push/replace
// re-parse the query string, bump a tick counter, and notify the
// subscribed components. Real Next.js does this via the App Router's
// navigation context; the mock has to recreate enough of that contract
// for ``useSearchParams`` to fire updates.
type Listener = () => void;
const navState: { params: URLSearchParams; listeners: Set<Listener> } = {
  params: new URLSearchParams(),
  listeners: new Set(),
};
const writeUrl = (url: unknown) => {
  if (typeof url !== 'string') return;
  const qs = url.includes('?') ? url.split('?', 2)[1] : '';
  navState.params = new URLSearchParams(qs);
  navState.listeners.forEach(l => l());
};

jest.mock('next/navigation', () => {
  const { useEffect, useState } =
    jest.requireActual<typeof import('react')>('react');
  return {
    useRouter: () => ({
      push: (...args: unknown[]) => {
        writeUrl(args[0]);
        mockReplace(...args);
      },
      replace: (...args: unknown[]) => {
        writeUrl(args[0]);
        mockReplace(...args);
      },
      refresh: jest.fn(),
      prefetch: jest.fn(),
      back: jest.fn(),
    }),
    useSearchParams: () => {
      const [, setTick] = useState(0);
      useEffect(() => {
        const listener = () => setTick(t => t + 1);
        navState.listeners.add(listener);
        return () => {
          navState.listeners.delete(listener);
        };
      }, []);
      return navState.params;
    },
    usePathname: () => '/jobs',
  };
});

jest.mock('@/state/Toast/ToastProvider', () => ({
  useToast: () => ({
    toast: (...args: unknown[]) => mockToast(...args),
  }),
}));

// Capture the most recent JobsListView props so each test can inspect /
// invoke the callbacks the parent passed in. We render minimal UI from the
// stub: a filter button (to verify onFiltersChange fires), a select-toggle
// button (onSelectionChange), and a posting row per item.
type JobsListViewSpyProps = {
  filters: JobsFilterState;
  onFiltersChange: (f: JobsFilterState) => void;
  selectedIds: Set<string>;
  onSelectionChange: (ids: Set<string>) => void;
  refreshKey: number;
  targetId: string | undefined;
  analysisTargetId: string | undefined;
  onPostingsLoaded?: ((postings: JobPosting[]) => void) | undefined;
};

let lastJobsListViewProps: JobsListViewSpyProps | null = null;
let mockPostings: JobPosting[] = [];
let mockLoading = false;

jest.mock('../JobsListView', () => ({
  __esModule: true,
  default: (props: JobsListViewSpyProps) => {
    lastJobsListViewProps = props;
    if (mockLoading) {
      return (
        <div data-testid='jobs-list-view-stub' aria-label='Loading jobs'>
          loading
        </div>
      );
    }
    if (mockPostings.length === 0) {
      return (
        <div data-testid='jobs-list-view-stub'>
          <p>No matching jobs</p>
        </div>
      );
    }
    return (
      <div data-testid='jobs-list-view-stub'>
        <button
          type='button'
          onClick={() =>
            props.onFiltersChange({
              ...props.filters,
              minScore: '70',
            })
          }
        >
          stub-change-filter
        </button>
        <ul>
          {mockPostings.map(p => (
            <li key={p.id} data-testid='posting-row'>
              <button
                type='button'
                aria-label={`Select ${p.title}`}
                onClick={() => {
                  const next = new Set(props.selectedIds);
                  if (next.has(p.id)) next.delete(p.id);
                  else next.add(p.id);
                  props.onSelectionChange(next);
                }}
              >
                {p.title}
              </button>
              <span data-testid={`score-${p.id}`}>{p.score}</span>
            </li>
          ))}
        </ul>
      </div>
    );
  },
}));

// BatchActionBar — stub renders a sentinel only when selectedCount > 0,
// matching the real component's "return null on 0" behaviour.
jest.mock('../BatchActionBar', () => ({
  __esModule: true,
  default: ({ selectedCount }: { selectedCount: number }) =>
    selectedCount === 0 ? null : (
      <div role='toolbar' aria-label='Batch actions'>
        {selectedCount} selected
      </div>
    ),
}));

const POSTINGS: JobPosting[] = [
  {
    id: 'job-1',
    external_id: 'ext-1',
    source_id: 'src-1',
    title: 'Senior Frontend Engineer',
    company_name: 'Acme',
    location: 'Remote',
    absolute_url: 'https://example.com/1',
    score: 82,
    score_breakdown: null,
    scoring_status: 'complete',
    status: 'new',
    salary_text: null,
    greenhouse_updated_at: null,
    first_seen_at: '2026-01-01T00:00:00Z',
    created_at: '2026-01-01T00:00:00Z',
  },
  {
    id: 'job-2',
    external_id: 'ext-2',
    source_id: 'src-1',
    title: 'Staff Engineer',
    company_name: 'Globex',
    location: null,
    absolute_url: null,
    score: 64,
    score_breakdown: null,
    scoring_status: 'complete',
    status: 'saved',
    salary_text: null,
    greenhouse_updated_at: null,
    first_seen_at: '2026-01-02T00:00:00Z',
    created_at: '2026-01-02T00:00:00Z',
  },
];

const ORIGINAL_FETCH = global.fetch;

afterAll(() => {
  global.fetch = ORIGINAL_FETCH;
});

beforeEach(() => {
  jest.clearAllMocks();
  mockPostings = [];
  mockLoading = false;
  lastJobsListViewProps = null;
  // Reset the URL-state mock between tests so test order doesn't matter.
  navState.params = new URLSearchParams();
  navState.listeners.clear();
  global.fetch = jest.fn().mockResolvedValue({
    ok: true,
    json: async () => ({ activation_status: 'ready', jobs_count: 0 }),
  }) as unknown as typeof fetch;
});

describe('JobsList — empty targets state', () => {
  it('renders the "No active targets" empty state with a CTA to /targets', () => {
    render(<JobsList targetId={undefined} initialTargets={[]} />);

    expect(screen.getByText(/no active targets/i)).toBeInTheDocument();
    expect(
      screen.getByRole('link', { name: /go to targets/i })
    ).toHaveAttribute('href', '/targets');
    expect(
      screen.queryByRole('group', { name: /filter jobs by target/i })
    ).not.toBeInTheDocument();
  });
});

describe('JobsList — with targets', () => {
  const TARGETS: TargetTab[] = [
    { id: 't1', label: 'Frontend' },
    { id: 't2', label: 'Backend' },
  ];

  it('renders the page heading and a target filter group with "All Jobs" + targets', () => {
    render(<JobsList targetId={undefined} initialTargets={TARGETS} />);

    expect(
      screen.getByRole('heading', { level: 1, name: /jobs/i })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('group', { name: /filter jobs by target/i })
    ).toBeInTheDocument();
    // No URL ``?target=`` was provided, so the page defaults to the
    // cross-target "All Jobs" view rather than auto-selecting the first
    // target. Per-target views require an explicit tab click (which
    // writes ``?target=X``), so a fresh ``/jobs`` landing no longer
    // pins users to a target they didn't pick.
    expect(screen.getByRole('button', { name: /all jobs/i })).toHaveAttribute(
      'aria-pressed',
      'true'
    );
    expect(screen.getByRole('button', { name: /^frontend$/i })).toHaveAttribute(
      'aria-pressed',
      'false'
    );
    expect(
      screen.getByRole('button', { name: /backend/i })
    ).toBeInTheDocument();
  });

  it('renders target tabs with a plain label and no paused affordance', () => {
    // Paused targets are omitted upstream (page.tsx → toActiveTargetTabs), so
    // JobsList is paused-agnostic: tabs are plain labels, never a "(paused)"
    // suffix, and there is no in-list reactivate banner (that lives on /targets).
    render(<JobsList targetId='t1' initialTargets={TARGETS} />);
    expect(
      screen.getByRole('button', { name: /^frontend$/i })
    ).toBeInTheDocument();
    expect(screen.queryByText(/paused/i)).toBeNull();
    expect(screen.queryByRole('button', { name: /^reactivate$/i })).toBeNull();
  });

  it('renders the loading skeleton state via JobsListView', () => {
    mockLoading = true;
    render(<JobsList targetId={undefined} initialTargets={TARGETS} />);

    expect(screen.getByLabelText(/loading jobs/i)).toBeInTheDocument();
  });

  it('renders an empty postings message when JobsListView reports no rows', () => {
    mockPostings = [];
    render(<JobsList targetId={undefined} initialTargets={TARGETS} />);

    expect(screen.getByText(/no matching jobs/i)).toBeInTheDocument();
  });

  it('renders one row per posting with its score badge', () => {
    mockPostings = POSTINGS;
    render(<JobsList targetId={undefined} initialTargets={TARGETS} />);

    expect(screen.getAllByTestId('posting-row')).toHaveLength(2);
    expect(screen.getByTestId('score-job-1')).toHaveTextContent('82');
    expect(screen.getByTestId('score-job-2')).toHaveTextContent('64');
    expect(
      screen.getByRole('button', { name: /select senior frontend engineer/i })
    ).toBeInTheDocument();
  });

  it('forwards filter changes from JobsListView (onFiltersChange wiring)', async () => {
    mockPostings = POSTINGS;
    const user = userEvent.setup();
    render(<JobsList targetId={undefined} initialTargets={TARGETS} />);

    expect(lastJobsListViewProps?.filters.minScore).toBe('');
    await user.click(
      screen.getByRole('button', { name: /stub-change-filter/i })
    );
    await waitFor(() => {
      expect(lastJobsListViewProps?.filters.minScore).toBe('70');
    });
  });

  it('hides the BatchActionBar when no rows are selected', () => {
    mockPostings = POSTINGS;
    render(<JobsList targetId={undefined} initialTargets={TARGETS} />);

    expect(
      screen.queryByRole('toolbar', { name: /batch actions/i })
    ).not.toBeInTheDocument();
  });

  it('shows the BatchActionBar after at least one posting is selected', async () => {
    mockPostings = POSTINGS;
    const user = userEvent.setup();
    render(<JobsList targetId={undefined} initialTargets={TARGETS} />);

    await user.click(
      screen.getByRole('button', { name: /select senior frontend engineer/i })
    );

    expect(
      await screen.findByRole('toolbar', { name: /batch actions/i })
    ).toHaveTextContent(/1 selected/i);
  });

  it('calls router.replace and resets selection when switching tabs', async () => {
    mockPostings = POSTINGS;
    const user = userEvent.setup();
    render(<JobsList targetId={undefined} initialTargets={TARGETS} />);

    await user.click(
      screen.getByRole('button', { name: /select senior frontend engineer/i })
    );
    await user.click(screen.getByRole('button', { name: /^frontend$/i }));

    expect(mockReplace).toHaveBeenCalledWith('/jobs?target=t1', {
      scroll: false,
    });
    // BatchActionBar disappears because selection was reset
    await waitFor(() => {
      expect(
        screen.queryByRole('toolbar', { name: /batch actions/i })
      ).not.toBeInTheDocument();
    });
  });
});

describe('JobsList — activation status poll cadence', () => {
  // The poll is a chained setTimeout: 3s ticks for the first 10 attempts,
  // then 15s, parked entirely while the tab is hidden. It replaced a flat
  // 3s setInterval that burned its whole 60-attempt budget in 3 minutes at
  // 20 req/min against a pipeline that legitimately runs for many minutes.
  const TARGETS: TargetTab[] = [{ id: 't1', label: 'Frontend' }];

  const pollingResponse = {
    ok: true,
    json: async () => ({ activation_status: 'polling', jobs_count: 0 }),
  };

  function statusCalls(spy: jest.Mock): number {
    return spy.mock.calls.filter(([url]) => String(url).includes('/status'))
      .length;
  }

  async function tick(ms: number) {
    await act(async () => {
      await jest.advanceTimersByTimeAsync(ms);
    });
  }

  afterEach(() => {
    jest.useRealTimers();
  });

  it('polls at 3s for the fast window, then backs off to 15s', async () => {
    jest.useFakeTimers();
    const fetchSpy = jest.fn().mockResolvedValue(pollingResponse);
    global.fetch = fetchSpy as unknown as typeof fetch;

    render(<JobsList targetId='t1' initialTargets={TARGETS} />);
    await tick(0); // initial check fires immediately
    expect(statusCalls(fetchSpy)).toBe(1);

    await tick(3000);
    expect(statusCalls(fetchSpy)).toBe(2);

    // Consume the rest of the fast window (attempts 3..10 at 3s each).
    await tick(8 * 3000);
    expect(statusCalls(fetchSpy)).toBe(10);

    // Backed off: 3s later nothing fires…
    await tick(3000);
    expect(statusCalls(fetchSpy)).toBe(10);
    // …the next tick lands at 15s.
    await tick(12000);
    expect(statusCalls(fetchSpy)).toBe(11);
  });

  it('parks the poll while the tab is hidden and resumes on visibilitychange', async () => {
    jest.useFakeTimers();
    const fetchSpy = jest.fn().mockResolvedValue(pollingResponse);
    global.fetch = fetchSpy as unknown as typeof fetch;
    const visibility = jest.spyOn(document, 'visibilityState', 'get');
    visibility.mockReturnValue('visible');

    try {
      render(<JobsList targetId='t1' initialTargets={TARGETS} />);
      await tick(0);
      expect(statusCalls(fetchSpy)).toBe(1);

      // Hide the tab: the pending tick fires but issues no request and
      // schedules nothing further — a backgrounded /jobs tab goes silent.
      visibility.mockReturnValue('hidden');
      await tick(60_000);
      expect(statusCalls(fetchSpy)).toBe(1);

      // Foreground again: the visibilitychange handler re-enters the loop
      // immediately (no stale multi-second wait for fresh status).
      visibility.mockReturnValue('visible');
      await act(async () => {
        document.dispatchEvent(new Event('visibilitychange'));
        await jest.advanceTimersByTimeAsync(0);
      });
      expect(statusCalls(fetchSpy)).toBe(2);
    } finally {
      visibility.mockRestore();
    }
  });

  it('survives a transient non-ok response and stops once ready', async () => {
    jest.useFakeTimers();
    const fetchSpy = jest
      .fn()
      .mockResolvedValueOnce(pollingResponse)
      .mockResolvedValueOnce({ ok: false }) // transient 5xx mid-pipeline
      .mockResolvedValue({
        ok: true,
        json: async () => ({ activation_status: 'ready', jobs_count: 3 }),
      });
    global.fetch = fetchSpy as unknown as typeof fetch;

    render(<JobsList targetId='t1' initialTargets={TARGETS} />);
    await tick(0); // polling
    await tick(3000); // 5xx — the chain must reschedule, not freeze
    await tick(3000); // ready → clears the poll
    expect(statusCalls(fetchSpy)).toBe(3);

    // Settled: no further ticks, ever.
    await tick(120_000);
    expect(statusCalls(fetchSpy)).toBe(3);
  });
});
