import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import TargetSearchTab from '../TargetSearchTab';
import type {
  JobTarget,
  MatchedSuggestion,
  TargetSearchResult,
} from '../types';

const ORIGINAL_FETCH = global.fetch;

/**
 * Route fetch by URL: the search box hits `GET /api/targets/search`, the AI
 * fallback hits `POST /api/targets/suggest-from-query`. Tests supply either.
 */
function mockRoutes(opts: {
  search?: TargetSearchResult[];
  suggest?: { matches: MatchedSuggestion[] };
  suggestFails?: boolean;
}) {
  const { search = [], suggest = { matches: [] }, suggestFails = false } = opts;
  global.fetch = jest.fn().mockImplementation((url: string) => {
    const u = String(url);
    if (u.includes('/api/targets/search')) {
      return Promise.resolve({
        ok: true,
        json: async () => ({ results: search }),
      });
    }
    if (u.includes('/api/targets/suggest-from-query')) {
      return Promise.resolve({
        ok: !suggestFails,
        json: async () => suggest,
      });
    }
    return Promise.reject(new Error(`unexpected fetch: ${u}`));
  }) as unknown as typeof fetch;
}

function newMatch(label: string): MatchedSuggestion {
  return {
    suggestion: {
      label,
      description: `${label} roles.`,
      core_skills: ['React'],
    },
    matched_target: null,
    is_new: true,
  };
}

function existingMatch(label: string): MatchedSuggestion {
  return {
    suggestion: { label, description: `${label} roles.`, core_skills: [] },
    matched_target: { id: 't-existing', label } as unknown as JobTarget,
    is_new: false,
  };
}

const searchBox = () =>
  screen.getByRole('textbox', { name: /search existing targets/i });

const noop = () => Promise.resolve(true);

afterEach(() => {
  global.fetch = ORIGINAL_FETCH;
  jest.clearAllMocks();
});

describe('TargetSearchTab', () => {
  it('prompts for a longer query and does not search on a single character', async () => {
    mockRoutes({ search: [] });
    const user = userEvent.setup();
    render(<TargetSearchTab onFollow={jest.fn()} onCreateSuggestion={noop} />);

    expect(screen.getByText(/type at least 2 characters/i)).toBeInTheDocument();

    await user.type(searchBox(), 'f');
    expect(global.fetch).not.toHaveBeenCalled(); // 1 char → no request
  });

  it('debounce-searches and renders results (Follow vs already-Following)', async () => {
    mockRoutes({
      search: [
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
      ],
    });
    const user = userEvent.setup();
    render(
      <TargetSearchTab
        onFollow={jest.fn().mockResolvedValue(true)}
        onCreateSuggestion={noop}
      />
    );

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
    mockRoutes({
      search: [
        {
          id: 't-1',
          label: 'Senior Frontend Engineer',
          description: null,
          is_linked: false,
        },
      ],
    });
    const onFollow = jest.fn().mockResolvedValue(true);
    const user = userEvent.setup();
    render(<TargetSearchTab onFollow={onFollow} onCreateSuggestion={noop} />);

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
    mockRoutes({
      search: [
        {
          id: 't-1',
          label: 'Senior Frontend Engineer',
          description: null,
          is_linked: false,
        },
      ],
    });
    const onFollow = jest.fn().mockResolvedValue(false); // e.g. active-target limit
    const user = userEvent.setup();
    render(<TargetSearchTab onFollow={onFollow} onCreateSuggestion={noop} />);

    await user.type(searchBox(), 'frontend');
    await user.click(await screen.findByRole('button', { name: /^follow$/i }));

    await waitFor(() => expect(onFollow).toHaveBeenCalled());
    // Follow failed → the button stays (no optimistic flip).
    expect(
      screen.getByRole('button', { name: /^follow$/i })
    ).toBeInTheDocument();
  });

  it('shows an empty-state when nothing matches', async () => {
    mockRoutes({ search: [] });
    const user = userEvent.setup();
    render(<TargetSearchTab onFollow={jest.fn()} onCreateSuggestion={noop} />);

    await user.type(searchBox(), 'zznomatch');

    expect(await screen.findByText(/no targets match/i)).toBeInTheDocument();
  });

  // ---- AI fallback (POST /api/targets/suggest-from-query) --------------------

  describe('AI suggestion fallback', () => {
    it('offers "Suggest with AI" in the empty state and renders suggestion cards', async () => {
      mockRoutes({
        search: [],
        suggest: {
          matches: [
            newMatch('Senior Frontend Engineer'),
            existingMatch('Staff Frontend Engineer'),
          ],
        },
      });
      const user = userEvent.setup();
      render(
        <TargetSearchTab onFollow={jest.fn()} onCreateSuggestion={noop} />
      );

      await user.type(searchBox(), 'senior frontend engineer');
      const trigger = await screen.findByRole('button', {
        name: /suggest roles with ai/i,
      });
      await user.click(trigger);

      // Cards for both suggestions, with Create (new) vs Follow (catalog match).
      expect(
        await screen.findByText('Senior Frontend Engineer')
      ).toBeInTheDocument();
      expect(screen.getByText('Staff Frontend Engineer')).toBeInTheDocument();
      expect(
        screen.getByRole('button', { name: /^create$/i })
      ).toBeInTheDocument();
      expect(
        screen.getByRole('button', { name: /^follow$/i })
      ).toBeInTheDocument();
      expect(global.fetch).toHaveBeenCalledWith(
        '/api/targets/suggest-from-query',
        expect.objectContaining({ method: 'POST' })
      );
    });

    it('creates a suggestion and removes its card on success', async () => {
      mockRoutes({
        search: [],
        suggest: { matches: [newMatch('Senior Frontend Engineer')] },
      });
      const onCreateSuggestion = jest.fn().mockResolvedValue(true);
      const user = userEvent.setup();
      render(
        <TargetSearchTab
          onFollow={jest.fn()}
          onCreateSuggestion={onCreateSuggestion}
        />
      );

      await user.type(searchBox(), 'senior frontend engineer');
      await user.click(
        await screen.findByRole('button', { name: /suggest roles with ai/i })
      );
      await user.click(
        await screen.findByRole('button', { name: /^create$/i })
      );

      await waitFor(() =>
        expect(onCreateSuggestion).toHaveBeenCalledWith(
          expect.objectContaining({
            suggestion: expect.objectContaining({
              label: 'Senior Frontend Engineer',
            }),
            is_new: true,
          })
        )
      );
      // Card removed after success.
      await waitFor(() =>
        expect(
          screen.queryByRole('button', { name: /^create$/i })
        ).not.toBeInTheDocument()
      );
    });

    it('keeps the card when creation fails', async () => {
      mockRoutes({
        search: [],
        suggest: { matches: [newMatch('Senior Frontend Engineer')] },
      });
      const onCreateSuggestion = jest.fn().mockResolvedValue(false);
      const user = userEvent.setup();
      render(
        <TargetSearchTab
          onFollow={jest.fn()}
          onCreateSuggestion={onCreateSuggestion}
        />
      );

      await user.type(searchBox(), 'senior frontend engineer');
      await user.click(
        await screen.findByRole('button', { name: /suggest roles with ai/i })
      );
      await user.click(
        await screen.findByRole('button', { name: /^create$/i })
      );

      await waitFor(() => expect(onCreateSuggestion).toHaveBeenCalled());
      // Failed → the card stays for a retry.
      expect(
        screen.getByRole('button', { name: /^create$/i })
      ).toBeInTheDocument();
    });

    it('shows a fallback message when the LLM returns no suggestions', async () => {
      mockRoutes({ search: [], suggest: { matches: [] } });
      const user = userEvent.setup();
      render(
        <TargetSearchTab onFollow={jest.fn()} onCreateSuggestion={noop} />
      );

      await user.type(searchBox(), 'zznorole');
      await user.click(
        await screen.findByRole('button', { name: /suggest roles with ai/i })
      );

      expect(
        await screen.findByText(/ai didn.t find a good role/i)
      ).toBeInTheDocument();
    });

    it('surfaces an error when the suggest request fails', async () => {
      mockRoutes({ search: [], suggestFails: true });
      const user = userEvent.setup();
      render(
        <TargetSearchTab onFollow={jest.fn()} onCreateSuggestion={noop} />
      );

      await user.type(searchBox(), 'senior frontend engineer');
      await user.click(
        await screen.findByRole('button', { name: /suggest roles with ai/i })
      );

      expect(
        await screen.findByText(/couldn.t generate suggestions/i)
      ).toBeInTheDocument();
    });

    it('offers a quieter "none of these?" AI affordance beneath thin catalog hits', async () => {
      mockRoutes({
        search: [
          {
            id: 't-1',
            label: 'Frontend Developer',
            description: null,
            is_linked: false,
          },
        ],
        suggest: { matches: [newMatch('Senior Frontend Engineer')] },
      });
      const user = userEvent.setup();
      render(
        <TargetSearchTab onFollow={jest.fn()} onCreateSuggestion={noop} />
      );

      await user.type(searchBox(), 'frontend');
      // A catalog hit renders...
      expect(await screen.findByText('Frontend Developer')).toBeInTheDocument();
      // ...plus the quieter AI affordance underneath.
      await user.click(
        await screen.findByRole('button', {
          name: /none of these\? suggest with ai/i,
        })
      );
      expect(
        await screen.findByText('Senior Frontend Engineer')
      ).toBeInTheDocument();
    });

    it('clears stale AI suggestions when the query changes', async () => {
      mockRoutes({
        search: [],
        suggest: { matches: [newMatch('Senior Frontend Engineer')] },
      });
      const user = userEvent.setup();
      render(
        <TargetSearchTab onFollow={jest.fn()} onCreateSuggestion={noop} />
      );

      await user.type(searchBox(), 'senior frontend engineer');
      await user.click(
        await screen.findByRole('button', { name: /suggest roles with ai/i })
      );
      expect(
        await screen.findByText('Senior Frontend Engineer')
      ).toBeInTheDocument();

      // Change the query → the AI card must disappear (belongs to old query).
      await user.type(searchBox(), ' remote');
      await waitFor(() =>
        expect(
          screen.queryByText('Senior Frontend Engineer')
        ).not.toBeInTheDocument()
      );
    });
  });
});
