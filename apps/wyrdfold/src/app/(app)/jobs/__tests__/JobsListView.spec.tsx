import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import JobsListView from '../JobsListView';

// Replace the heavy children with simple identifiable stubs so the spec
// can exercise the layout-switch logic without pulling in the table /
// mobile internals (those are covered in their own specs once authored).
// The stubs expose an ``onRefetch`` trigger so the spec can assert the
// refetch path issues exactly one ``/api/jobs`` GET (no duplicate).
jest.mock('../JobsListTable', () => ({
  __esModule: true,
  default: ({ onRefetch }: { onRefetch: () => void }) => (
    <div data-testid='jobs-list-table'>
      <button type='button' data-testid='table-refetch' onClick={onRefetch}>
        refetch
      </button>
    </div>
  ),
}));

jest.mock('../JobsListMobile', () => ({
  __esModule: true,
  default: ({ onRefetch }: { onRefetch: () => void }) => (
    <div data-testid='jobs-list-mobile'>
      <button type='button' data-testid='mobile-refetch' onClick={onRefetch}>
        refetch
      </button>
    </div>
  ),
}));

jest.mock('../JobsFilter', () => ({
  __esModule: true,
  default: () => <div data-testid='jobs-filter' />,
}));

const originalFetch = global.fetch;
beforeEach(() => {
  global.fetch = jest.fn().mockResolvedValue({
    ok: true,
    json: async () => ({ postings: [], total: 0 }),
  } as Response) as unknown as typeof fetch;
});
afterEach(() => {
  global.fetch = originalFetch;
});

function setMatchMedia(matches: boolean) {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: (query: string) => ({
      matches,
      media: query,
      onchange: null,
      addListener: jest.fn(),
      removeListener: jest.fn(),
      addEventListener: jest.fn(),
      removeEventListener: jest.fn(),
      dispatchEvent: jest.fn(),
    }),
  });
}

const baseProps = {
  filters: {
    minScore: '',
    status: '',
    search: '',
    excludeLocations: '',
    onlyLocations: '',
  },
  onFiltersChange: () => undefined,
  selectedIds: new Set<string>(),
  onSelectionChange: () => undefined,
  refreshKey: 0,
  targetId: undefined,
  analysisTargetId: undefined,
};

describe('JobsListView', () => {
  it('renders the filter and the mobile list on narrow viewports', async () => {
    setMatchMedia(false);
    render(<JobsListView {...baseProps} />);
    expect(screen.getByTestId('jobs-filter')).toBeInTheDocument();
    expect(screen.getByTestId('jobs-list-mobile')).toBeInTheDocument();
    expect(screen.queryByTestId('jobs-list-table')).toBeNull();
  });

  it('renders the desktop table on wide viewports', async () => {
    setMatchMedia(true);
    render(<JobsListView {...baseProps} />);
    expect(screen.getByTestId('jobs-filter')).toBeInTheDocument();
    // Table is dynamically loaded — wait for it to mount.
    await waitFor(() => {
      expect(screen.getByTestId('jobs-list-table')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('jobs-list-mobile')).toBeNull();
  });

  it('issues exactly ONE /api/jobs fetch per refetch (no duplicate)', async () => {
    // Regression guard: ``handleRefetch`` used to bump the cache-buster
    // (``_r``, which re-keys ``buildUrl`` → effect refetches once) AND call
    // ``refetch()`` explicitly — firing ``GET /api/jobs`` twice per
    // status-change / target-chip / delete. It should fire exactly once.
    const user = userEvent.setup();
    setMatchMedia(false); // mobile stub renders synchronously
    render(<JobsListView {...baseProps} />);

    const jobsCalls = () =>
      (global.fetch as jest.Mock).mock.calls.filter(([url]) =>
        String(url).startsWith('/api/jobs')
      );

    // Initial mount fetch settles.
    await waitFor(() => expect(jobsCalls().length).toBe(1));

    await user.click(screen.getByTestId('mobile-refetch'));

    await waitFor(() => expect(jobsCalls().length).toBe(2));
    // Give any stray second fetch a chance to land, then confirm it didn't.
    await Promise.resolve();
    expect(jobsCalls().length).toBe(2);
  });
});
