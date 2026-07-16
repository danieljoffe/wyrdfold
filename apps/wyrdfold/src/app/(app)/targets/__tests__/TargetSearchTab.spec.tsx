import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import TargetSearchTab from '../TargetSearchTab';
import type { TargetSearchResult } from '../types';

const ORIGINAL_FETCH = global.fetch;

function mockSearch(results: TargetSearchResult[]) {
  global.fetch = jest.fn().mockResolvedValue({
    ok: true,
    json: async () => ({ results }),
  }) as unknown as typeof fetch;
}

const searchBox = () =>
  screen.getByRole('textbox', { name: /search existing targets/i });

afterEach(() => {
  global.fetch = ORIGINAL_FETCH;
  jest.clearAllMocks();
});

describe('TargetSearchTab', () => {
  it('prompts for a longer query and does not search on a single character', async () => {
    mockSearch([]);
    const user = userEvent.setup();
    render(<TargetSearchTab onFollow={jest.fn()} />);

    expect(screen.getByText(/type at least 2 characters/i)).toBeInTheDocument();

    await user.type(searchBox(), 'f');
    expect(global.fetch).not.toHaveBeenCalled(); // 1 char → no request
  });

  it('debounce-searches and renders results (Follow vs already-Following)', async () => {
    mockSearch([
      {
        id: 't-1',
        label: 'Senior Frontend Engineer',
        description: 'react roles',
        is_linked: false,
      },
      {
        id: 't-2',
        label: 'Staff Frontend Engineer',
        description: null,
        is_linked: true,
      },
    ]);
    const user = userEvent.setup();
    render(<TargetSearchTab onFollow={jest.fn().mockResolvedValue(true)} />);

    await user.type(searchBox(), 'frontend');

    expect(
      await screen.findByText('Senior Frontend Engineer')
    ).toBeInTheDocument();
    // Not-followed → a Follow button; already-followed → a "Following" label.
    expect(
      screen.getByRole('button', { name: /^follow$/i })
    ).toBeInTheDocument();
    expect(screen.getByText(/^following$/i)).toBeInTheDocument();
    expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/targets/search?q=frontend')
    );
  });

  it('follows a result and flips it to Following on success', async () => {
    mockSearch([
      {
        id: 't-1',
        label: 'Senior Frontend Engineer',
        description: null,
        is_linked: false,
      },
    ]);
    const onFollow = jest.fn().mockResolvedValue(true);
    const user = userEvent.setup();
    render(<TargetSearchTab onFollow={onFollow} />);

    await user.type(searchBox(), 'frontend');
    await user.click(await screen.findByRole('button', { name: /^follow$/i }));

    await waitFor(() =>
      expect(onFollow).toHaveBeenCalledWith(
        expect.objectContaining({ id: 't-1' })
      )
    );
    // The Follow button is replaced by the "Following" label.
    await waitFor(() =>
      expect(
        screen.queryByRole('button', { name: /^follow$/i })
      ).not.toBeInTheDocument()
    );
    expect(screen.getByText(/^following$/i)).toBeInTheDocument();
  });

  it('does not flip to Following when the follow fails', async () => {
    mockSearch([
      {
        id: 't-1',
        label: 'Senior Frontend Engineer',
        description: null,
        is_linked: false,
      },
    ]);
    const onFollow = jest.fn().mockResolvedValue(false); // e.g. active-target limit
    const user = userEvent.setup();
    render(<TargetSearchTab onFollow={onFollow} />);

    await user.type(searchBox(), 'frontend');
    await user.click(await screen.findByRole('button', { name: /^follow$/i }));

    await waitFor(() => expect(onFollow).toHaveBeenCalled());
    // Follow failed → the button stays (no optimistic flip).
    expect(
      screen.getByRole('button', { name: /^follow$/i })
    ).toBeInTheDocument();
  });

  it('shows an empty-state when nothing matches', async () => {
    mockSearch([]);
    const user = userEvent.setup();
    render(<TargetSearchTab onFollow={jest.fn()} />);

    await user.type(searchBox(), 'zznomatch');

    expect(await screen.findByText(/no targets match/i)).toBeInTheDocument();
  });
});
