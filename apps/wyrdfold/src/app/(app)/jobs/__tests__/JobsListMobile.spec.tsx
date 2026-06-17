import React from 'react';
import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import JobsListMobile from '../JobsListMobile';
import type { JobPosting } from '../types';

const mockToast = jest.fn();
jest.mock('@/state/Toast/ToastProvider', () => ({
  useToast: () => ({ toast: mockToast }),
}));

const mockPush = jest.fn();
jest.mock('next/navigation', () => ({
  useRouter: () => ({ push: mockPush, prefetch: jest.fn() }),
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
    greenhouse_updated_at: null,
    first_seen_at: '2026-01-01',
    created_at: '2026-01-01',
    ...overrides,
  };
}

beforeEach(() => {
  jest.clearAllMocks();
});

describe('JobsListMobile', () => {
  it('shows the loading skeleton when loading and no postings yet', () => {
    render(
      <JobsListMobile
        postings={[]}
        loading
        hasMore={false}
        loadingMore={false}
        onLoadMore={() => undefined}
        selectedIds={new Set()}
        onSelectionChange={() => undefined}
        onRefetch={() => undefined}
      />
    );
    expect(screen.getByLabelText(/loading jobs/i)).toBeInTheDocument();
  });

  it('renders the empty-state message when there are no postings', () => {
    render(
      <JobsListMobile
        postings={[]}
        loading={false}
        hasMore={false}
        loadingMore={false}
        onLoadMore={() => undefined}
        selectedIds={new Set()}
        onSelectionChange={() => undefined}
        onRefetch={() => undefined}
      />
    );
    expect(screen.getByText(/no jobs found/i)).toBeInTheDocument();
  });

  it('renders one JobCard per posting and forwards selection toggle', async () => {
    const onSelectionChange = jest.fn();
    const user = userEvent.setup();
    render(
      <JobsListMobile
        postings={[makeJob(), makeJob({ id: 'j-2', title: 'Backend Dev' })]}
        loading={false}
        hasMore={false}
        loadingMore={false}
        onLoadMore={() => undefined}
        selectedIds={new Set()}
        onSelectionChange={onSelectionChange}
        onRefetch={() => undefined}
      />
    );
    expect(
      screen.getByRole('button', { name: /senior frontend engineer at acme/i })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /backend dev at acme/i })
    ).toBeInTheDocument();

    await user.click(
      screen.getByRole('checkbox', { name: /select senior frontend engineer/i })
    );
    expect(onSelectionChange).toHaveBeenCalled();
  });

  it('hides the load-more button when there are no more pages', () => {
    render(
      <JobsListMobile
        postings={[makeJob()]}
        loading={false}
        hasMore={false}
        loadingMore={false}
        onLoadMore={() => undefined}
        selectedIds={new Set()}
        onSelectionChange={() => undefined}
        onRefetch={() => undefined}
      />
    );
    expect(
      screen.queryByRole('button', { name: /load more/i })
    ).not.toBeInTheDocument();
  });

  it('shows the load-more button and fires onLoadMore when there is a next page', async () => {
    const onLoadMore = jest.fn();
    const user = userEvent.setup();
    render(
      <JobsListMobile
        postings={[makeJob()]}
        loading={false}
        hasMore
        loadingMore={false}
        onLoadMore={onLoadMore}
        selectedIds={new Set()}
        onSelectionChange={() => undefined}
        onRefetch={() => undefined}
      />
    );
    await user.click(screen.getByRole('button', { name: /load more/i }));
    expect(onLoadMore).toHaveBeenCalled();
  });
});
