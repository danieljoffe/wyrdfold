import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import JobsListTable from '../JobsListTable';
import type { JobPosting, JobsSortColumn } from '../types';

// JobDetailPanel pulls in network + toast; stub it so this spec stays focused
// on the table's own sorting / selection / row-toggle behaviour.
jest.mock('../JobDetailPanel', () => ({
  __esModule: true,
  default: () => <div data-testid='job-detail-panel-stub' />,
}));

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

const baseProps = {
  hasMore: false,
  loadingMore: false,
  onLoadMore: () => undefined,
  sort: 'score' as JobsSortColumn,
  order: 'desc' as const,
  handleSort: () => undefined,
  sortIndicator: () => '',
  nextSortAction: () => 'descending' as const,
  analysisTargetId: undefined,
  onRefetch: () => undefined,
};

describe('JobsListTable', () => {
  it('renders the loading skeleton when loading with no postings', () => {
    render(
      <JobsListTable
        {...baseProps}
        postings={[]}
        loading
        selectedIds={new Set()}
        onSelectionChange={() => undefined}
      />
    );
    expect(screen.getByLabelText(/loading jobs/i)).toBeInTheDocument();
  });

  it('renders the empty state when there are no postings', () => {
    render(
      <JobsListTable
        {...baseProps}
        postings={[]}
        loading={false}
        selectedIds={new Set()}
        onSelectionChange={() => undefined}
      />
    );
    expect(screen.getByText(/no jobs found/i)).toBeInTheDocument();
  });

  it('renders sortable column headers with accessible sort buttons', () => {
    render(
      <JobsListTable
        {...baseProps}
        postings={[makeJob()]}
        loading={false}
        selectedIds={new Set()}
        onSelectionChange={() => undefined}
      />
    );
    expect(
      screen.getByRole('button', { name: /sort by score/i })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /sort by title/i })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /sort by company/i })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /sort by posted/i })
    ).toBeInTheDocument();
  });

  it('marks the active column with aria-sort matching the order prop', () => {
    render(
      <JobsListTable
        {...baseProps}
        sort='title'
        order='asc'
        postings={[makeJob()]}
        loading={false}
        selectedIds={new Set()}
        onSelectionChange={() => undefined}
      />
    );
    const sortBtn = screen.getByRole('button', { name: /sort by title/i });
    const header = sortBtn.closest('th');
    expect(header).toHaveAttribute('aria-sort', 'ascending');
  });

  it('renders compact logistics chips inline in the row when present (#86)', () => {
    // The desktop table row was the gap — mobile card + detail panel already
    // showed these; surfaced by the release end-to-end UX walkthrough.
    render(
      <JobsListTable
        {...baseProps}
        postings={[
          makeJob({
            logistics_filters: {
              remote_status: 'remote',
              salary_min: 150000,
              salary_max: 180000,
              salary_currency: 'USD',
              salary_unit: 'year',
              location_city: null,
              location_country: 'US',
            },
          }),
        ]}
        loading={false}
        selectedIds={new Set()}
        onSelectionChange={() => undefined}
      />
    );
    // Scope to the chip region — "Remote"/"US" also appear in the Location column.
    const chips = within(screen.getByLabelText('Job logistics'));
    expect(chips.getByText('Remote')).toBeInTheDocument();
    expect(chips.getByText('$150k–$180k')).toBeInTheDocument();
    expect(chips.getByText('US')).toBeInTheDocument();
  });

  it('invokes handleSort with the column key when a header button is clicked', async () => {
    const handleSort = jest.fn();
    const user = userEvent.setup();
    render(
      <JobsListTable
        {...baseProps}
        handleSort={handleSort}
        postings={[makeJob()]}
        loading={false}
        selectedIds={new Set()}
        onSelectionChange={() => undefined}
      />
    );
    await user.click(screen.getByRole('button', { name: /sort by score/i }));
    expect(handleSort).toHaveBeenCalledWith('score');
  });

  it('toggles select-all on the page when the header checkbox is clicked', async () => {
    const onSelectionChange = jest.fn();
    const user = userEvent.setup();
    const postings = [makeJob(), makeJob({ id: 'j-2', title: 'Other Role' })];
    render(
      <JobsListTable
        {...baseProps}
        postings={postings}
        loading={false}
        selectedIds={new Set()}
        onSelectionChange={onSelectionChange}
      />
    );
    await user.click(
      screen.getByRole('checkbox', { name: /select all on this page/i })
    );
    const next = onSelectionChange.mock.calls.at(-1)?.[0] as Set<string>;
    expect(next.has('j-1')).toBe(true);
    expect(next.has('j-2')).toBe(true);
  });

  it('toggles individual row selection without expanding the row', async () => {
    const onSelectionChange = jest.fn();
    const user = userEvent.setup();
    render(
      <JobsListTable
        {...baseProps}
        postings={[makeJob()]}
        loading={false}
        selectedIds={new Set()}
        onSelectionChange={onSelectionChange}
      />
    );

    await user.click(
      screen.getByRole('checkbox', { name: /select senior frontend engineer/i })
    );
    expect(onSelectionChange).toHaveBeenCalled();
    // Row should NOT have expanded the detail panel.
    expect(
      screen.queryByTestId('job-detail-panel-stub')
    ).not.toBeInTheDocument();
  });

  it('expands the detail panel when a row is clicked and collapses on second click', async () => {
    const user = userEvent.setup();
    render(
      <JobsListTable
        {...baseProps}
        postings={[makeJob()]}
        loading={false}
        selectedIds={new Set()}
        onSelectionChange={() => undefined}
      />
    );

    const row = screen.getByRole('row', {
      name: /senior frontend engineer at acme/i,
    });
    await user.click(row);
    expect(screen.getByTestId('job-detail-panel-stub')).toBeInTheDocument();
    expect(row).toHaveAttribute('aria-expanded', 'true');

    await user.click(row);
    expect(
      screen.queryByTestId('job-detail-panel-stub')
    ).not.toBeInTheDocument();
  });

  it('marks all-on-page checkbox as checked when every posting is selected', () => {
    const postings = [makeJob(), makeJob({ id: 'j-2', title: 'Other Role' })];
    render(
      <JobsListTable
        {...baseProps}
        postings={postings}
        loading={false}
        selectedIds={new Set(['j-1', 'j-2'])}
        onSelectionChange={() => undefined}
      />
    );
    expect(
      screen.getByRole('checkbox', { name: /select all on this page/i })
    ).toBeChecked();
  });

  it('renders an external link for postings with absolute_url', () => {
    render(
      <JobsListTable
        {...baseProps}
        postings={[makeJob({ absolute_url: 'https://example.com/job' })]}
        loading={false}
        selectedIds={new Set()}
        onSelectionChange={() => undefined}
      />
    );
    const link = screen.getByRole('link', {
      name: /senior frontend engineer/i,
    });
    expect(link).toHaveAttribute('href', 'https://example.com/job');
    expect(link).toHaveAttribute('target', '_blank');
  });

  it('renders the Discovered badge for manually-sourced jobs', () => {
    render(
      <JobsListTable
        {...baseProps}
        postings={[
          makeJob({ source_id: '00000000-0000-4000-a000-000000000001' }),
        ]}
        loading={false}
        selectedIds={new Set()}
        onSelectionChange={() => undefined}
      />
    );
    const row = screen.getByRole('row', {
      name: /senior frontend engineer at acme/i,
    });
    expect(within(row).getByText(/discovered/i)).toBeInTheDocument();
  });
});

describe('JobsListTable score cell (issue #603)', () => {
  // Prod regression 2026-08-05: the API marks fully-graded rows
  // `pending: false` but stamps scoring_status values like 'stage2';
  // the table omitted the authoritative `pending` prop, so ScoreBadge's
  // status heuristic classified every graded row as still scoring —
  // "·" placeholder + infinite spinner across the whole grid.
  it('renders the numeric score for a fit-graded row whose scoring_status is not "complete"', () => {
    render(
      <JobsListTable
        {...baseProps}
        postings={[
          makeJob({ score: 100, scoring_status: 'stage2', pending: false }),
        ]}
        loading={false}
        selectedIds={new Set()}
        onSelectionChange={() => undefined}
      />
    );
    expect(screen.getByLabelText('Match score 100')).toBeInTheDocument();
    expect(
      screen.queryByLabelText(/scoring in progress/i)
    ).not.toBeInTheDocument();
    expect(
      screen.queryByLabelText(/fit score pending/i)
    ).not.toBeInTheDocument();
  });

  it('keeps the pending placeholder for a row the API marks pending', () => {
    render(
      <JobsListTable
        {...baseProps}
        postings={[
          makeJob({ score: 61, scoring_status: 'stage2', pending: true }),
        ]}
        loading={false}
        selectedIds={new Set()}
        onSelectionChange={() => undefined}
      />
    );
    expect(screen.getByLabelText('Match score pending')).toBeInTheDocument();
    expect(screen.queryByLabelText('Match score 61')).not.toBeInTheDocument();
  });
});

describe('JobsListTable expanded-row pinning (issue #602)', () => {
  // Prod regression 2026-08-05: the panel's own onAnalysisComplete refetch
  // re-sorts the list; a negative fit grade drops the job off the page and
  // the open panel unmounted mid-read (twice observed, once mid resume
  // generation). The table now pins a snapshot of the expanded posting
  // until the user closes it.
  const jobA = () =>
    makeJob({ id: 'j-a', title: 'Vanishing Role', company_name: 'Acme' });
  const jobB = () =>
    makeJob({ id: 'j-b', title: 'Other Role', company_name: 'Beta' });

  function renderTable(postings: JobPosting[]) {
    const utils = render(
      <JobsListTable
        {...baseProps}
        postings={postings}
        loading={false}
        selectedIds={new Set()}
        onSelectionChange={() => undefined}
      />
    );
    const rerenderWith = (next: JobPosting[]) =>
      utils.rerender(
        <JobsListTable
          {...baseProps}
          postings={next}
          loading={false}
          selectedIds={new Set()}
          onSelectionChange={() => undefined}
        />
      );
    return { ...utils, rerenderWith };
  }

  it('keeps the expanded row and panel mounted when a refetch drops the job from the page', async () => {
    const user = userEvent.setup();
    const { rerenderWith } = renderTable([jobA(), jobB()]);

    await user.click(
      screen.getByRole('row', { name: /vanishing role at acme/i })
    );
    expect(screen.getByTestId('job-detail-panel-stub')).toBeInTheDocument();

    rerenderWith([jobB()]);

    expect(
      screen.getByRole('row', { name: /vanishing role at acme/i })
    ).toBeInTheDocument();
    expect(screen.getByTestId('job-detail-panel-stub')).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent(
      /re-ranked out of the current list/i
    );
  });

  it('drops the pinned row once the user collapses it', async () => {
    const user = userEvent.setup();
    const { rerenderWith } = renderTable([jobA(), jobB()]);

    await user.click(
      screen.getByRole('row', { name: /vanishing role at acme/i })
    );
    rerenderWith([jobB()]);

    await user.click(
      screen.getByRole('row', { name: /vanishing role at acme/i })
    );

    expect(
      screen.queryByRole('row', { name: /vanishing role at acme/i })
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId('job-detail-panel-stub')
    ).not.toBeInTheDocument();
  });

  it('shows no pin notice while the expanded job is still on the page', async () => {
    const user = userEvent.setup();
    renderTable([jobA(), jobB()]);

    await user.click(
      screen.getByRole('row', { name: /vanishing role at acme/i })
    );

    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });
});

describe('JobsListTable load-error state (issue #604)', () => {
  it('renders the load-error state, not "No jobs found", when the fetch failed', async () => {
    const user = userEvent.setup();
    const onRefetch = jest.fn();
    render(
      <JobsListTable
        {...baseProps}
        onRefetch={onRefetch}
        postings={[]}
        loading={false}
        loadError='Failed to load. Please try again.'
        selectedIds={new Set()}
        onSelectionChange={() => undefined}
      />
    );

    expect(screen.getByRole('alert')).toHaveTextContent(/loading problem/i);
    expect(screen.queryByText(/no jobs found/i)).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /retry/i }));
    expect(onRefetch).toHaveBeenCalled();
  });

  it('still renders the empty state for a genuinely empty result', () => {
    render(
      <JobsListTable
        {...baseProps}
        postings={[]}
        loading={false}
        selectedIds={new Set()}
        onSelectionChange={() => undefined}
      />
    );
    expect(screen.getByText(/no jobs found/i)).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});

// Keyboard selection. The row is a focusable widget whose Space/Enter expands
// it — but the handler lives on the <tr>, so a keypress originating in the
// row's OWN checkbox bubbles up to it. Mouse users are fine (the checkbox sits
// in a stopPropagation wrapper); keyboard users were not.
//
// This matters more since bulk actions shipped: a user who cannot select rows
// cannot use them at all.
describe('JobsListTable — keyboard selection', () => {
  function renderRow(onSelectionChange: (ids: Set<string>) => void) {
    render(
      <JobsListTable
        {...baseProps}
        postings={[makeJob()]}
        loading={false}
        selectedIds={new Set()}
        onSelectionChange={onSelectionChange}
      />
    );
  }

  it('selects the row when Space is pressed on its checkbox', async () => {
    const onSelectionChange = jest.fn();
    const user = userEvent.setup();
    renderRow(onSelectionChange);

    const box = screen.getByLabelText(/select senior frontend engineer/i);
    box.focus();
    await user.keyboard(' ');

    expect(onSelectionChange).toHaveBeenCalled();
    expect([...onSelectionChange.mock.calls[0][0]]).toEqual(['j-1']);
  });

  it('still expands the row when Space is pressed on the row itself', async () => {
    const user = userEvent.setup();
    renderRow(() => undefined);

    const row = screen.getByRole('row', { name: /senior frontend engineer at acme/i });
    row.focus();
    await user.keyboard(' ');

    expect(await screen.findByTestId('job-detail-panel-stub')).toBeInTheDocument();
  });
});
