import React from 'react';
import '@testing-library/jest-dom';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import TargetsList from '../TargetsList';
import {
  emptyScoringProfile,
  toSummary,
  type JobTarget,
  type UserTarget,
  type UserTargetWithSummary,
  type UserTargetWithTarget,
} from '../types';

const mockPush = jest.fn();
const mockRefresh = jest.fn();
jest.mock('next/navigation', () => ({
  useRouter: () => ({
    push: mockPush,
    prefetch: jest.fn(),
    refresh: mockRefresh,
  }),
}));

const mockToast = jest.fn();
jest.mock('@/state/Toast/ToastProvider', () => ({
  useToast: () => ({ toast: mockToast }),
}));

// CreateTargetModal renders a Dialog/Portal; stub it out so the list test
// stays focused on the list surface (modal mechanics covered separately).
jest.mock('../CreateTargetModal', () => ({
  __esModule: true,
  default: () => null,
}));

function makeEntry(
  id: string,
  label: string,
  overrides: {
    activation_status?: string;
    fit_score?: number | null;
    categories?: JobTarget['scoring_profile']['categories'];
  } = {}
): UserTargetWithTarget {
  const target: JobTarget = {
    id,
    label,
    description: null,
    normalized_label: null,
    scoring_profile: {
      ...emptyScoringProfile(),
      categories: overrides.categories ?? {},
    },
    search_keywords: [],
    activation_status: overrides.activation_status ?? 'ready',
    profile_version: 1,
    is_active: true,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-04-30T00:00:00Z',
  };
  const userTarget: UserTarget = {
    id: `u-${id}`,
    user_id: 'user',
    target_id: id,
    is_active: true,
    fit_score: overrides.fit_score ?? null,
    fit_score_reasoning: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-04-30T00:00:00Z',
  };
  return { user_target: userTarget, target };
}

/** List state holds summaries (#863) — the server `/targets/mine` feed and
 * the create/poll boundaries all project full targets down via `toSummary`. */
function makeSummaryEntry(
  ...args: Parameters<typeof makeEntry>
): UserTargetWithSummary {
  const entry = makeEntry(...args);
  return { user_target: entry.user_target, target: toSummary(entry.target) };
}

describe('TargetsList', () => {
  it('renders an empty-state CTA cluster when there are no targets', () => {
    render(<TargetsList initialTargets={[]} />);
    expect(screen.getByText(/No targets yet/i)).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /create target/i })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /suggest from experience/i })
    ).toBeInTheDocument();
  });

  it('renders one card per target plus add/suggest buttons below the grid when populated', () => {
    render(
      <TargetsList
        initialTargets={[
          makeSummaryEntry('t-1', 'Senior Frontend Engineer'),
          makeSummaryEntry('t-2', 'Full Stack Engineer'),
        ]}
      />
    );
    expect(screen.getByText('Senior Frontend Engineer')).toBeInTheDocument();
    expect(screen.getByText('Full Stack Engineer')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /^add target$/i })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /suggest from experience/i })
    ).toBeInTheDocument();
  });

  describe('deriving polling', () => {
    const originalFetch = global.fetch;
    afterEach(() => {
      jest.useRealTimers();
      global.fetch = originalFetch;
    });

    function mockFetchResolving(entry: UserTargetWithTarget): jest.Mock {
      const fetchMock = jest.fn().mockResolvedValue({
        ok: true,
        json: async () => entry,
      });
      global.fetch = fetchMock as unknown as typeof fetch;
      return fetchMock;
    }

    it('polls a deriving target and swaps in the derived profile + fit score', async () => {
      jest.useFakeTimers();
      const derived = makeEntry('t-1', 'Senior Frontend Engineer', {
        activation_status: 'ready',
        fit_score: 88,
        categories: {
          frontend: {
            keywords: { react: 3, typescript: 2, vue: 1, svelte: 1 },
            weight: 1,
          },
        },
      });
      const fetchMock = mockFetchResolving(derived);

      render(
        <TargetsList
          initialTargets={[
            makeSummaryEntry('t-1', 'Senior Frontend Engineer', {
              activation_status: 'deriving',
              fit_score: null,
            }),
          ]}
        />
      );

      // Starts in the deriving state.
      expect(screen.getByText(/building/i)).toBeInTheDocument();

      // Advance to the first poll; the settled response replaces the card.
      await act(async () => {
        await jest.advanceTimersByTimeAsync(2500);
      });

      expect(fetchMock).toHaveBeenCalledWith('/api/targets/t-1/user-target');
      await waitFor(() => {
        expect(screen.queryByText(/building/i)).toBeNull();
      });
      expect(screen.getByText('88')).toBeInTheDocument();
      // Counts derived from the full /user-target response via toSummary.
      expect(screen.getByText('4')).toBeInTheDocument(); // keyword count
    });

    it('stops polling once the target settles', async () => {
      jest.useFakeTimers();
      const derived = makeEntry('t-1', 'Senior Frontend Engineer', {
        activation_status: 'ready',
        fit_score: 75,
        categories: { frontend: { keywords: { react: 3 }, weight: 1 } },
      });
      const fetchMock = mockFetchResolving(derived);

      render(
        <TargetsList
          initialTargets={[
            makeSummaryEntry('t-1', 'Senior Frontend Engineer', {
              activation_status: 'deriving',
            }),
          ]}
        />
      );

      await act(async () => {
        await jest.advanceTimersByTimeAsync(2500);
      });
      const callsAfterSettle = fetchMock.mock.calls.length;

      // Further time passes — no additional polls once settled.
      await act(async () => {
        await jest.advanceTimersByTimeAsync(2500 * 3);
      });
      expect(fetchMock.mock.calls.length).toBe(callsAfterSettle);
    });
  });

  describe('activate / deactivate', () => {
    const originalFetch = global.fetch;
    afterEach(() => {
      mockToast.mockReset();
      mockRefresh.mockReset();
      global.fetch = originalFetch;
    });

    /** Open the per-card dropdown (kebab `aria-haspopup=menu` trigger, which
     * has no accessible name) and click the named menu item. */
    async function clickMenuAction(name: RegExp): Promise<void> {
      const user = userEvent.setup();
      const trigger = screen
        .getAllByRole('button')
        .find(b => b.getAttribute('aria-haspopup') === 'menu');
      if (!trigger) throw new Error('dropdown trigger not found');
      await user.click(trigger);
      const item = await screen.findByRole('menuitem', { name });
      await user.click(item);
    }

    it('flips the badge optimistically and does NOT call router.refresh on activate', async () => {
      // Activate POST never resolves during the assertion window, so any
      // badge flip we observe is purely optimistic.
      let resolveActivate: (v: unknown) => void = () => undefined;
      const activatePromise = new Promise(res => {
        resolveActivate = res;
      });
      const fetchMock = jest.fn((url: string) => {
        if (url.endsWith('/activate')) return activatePromise;
        if (url.endsWith('/user-target'))
          return Promise.resolve({
            ok: true,
            json: async () =>
              makeEntry('t-1', 'Senior Frontend Engineer', {
                activation_status: 'ready',
                fit_score: 80,
              }),
          });
        return Promise.resolve({ ok: true, json: async () => ({}) });
      });
      global.fetch = fetchMock as unknown as typeof fetch;

      // Seed an INACTIVE target so the menu offers "Activate".
      const entry = makeSummaryEntry('t-1', 'Senior Frontend Engineer', {
        activation_status: 'ready',
        fit_score: 80,
      });
      entry.user_target.is_active = false;
      render(<TargetsList initialTargets={[entry]} />);

      expect(screen.getByText(/inactive/i)).toBeInTheDocument();

      await clickMenuAction(/^Activate$/i);

      // Badge flips immediately, before the POST resolves.
      await waitFor(() => {
        expect(screen.getByText(/^Active$/i)).toBeInTheDocument();
      });
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/targets/t-1/activate',
        expect.objectContaining({ method: 'POST' })
      );
      // No blanket RSC refresh / nav re-prefetch for a single toggle.
      expect(mockRefresh).not.toHaveBeenCalled();

      // Let the POST + reconcile settle so the test exits cleanly.
      await act(async () => {
        resolveActivate({ ok: true, json: async () => ({}) });
      });
    });

    it('rolls back the optimistic flip and toasts on error', async () => {
      const fetchMock = jest.fn((url: string) => {
        if (url.endsWith('/deactivate'))
          return Promise.resolve({
            ok: false,
            json: async () => ({ detail: 'nope' }),
            text: async () => 'nope',
          });
        return Promise.resolve({ ok: true, json: async () => ({}) });
      });
      global.fetch = fetchMock as unknown as typeof fetch;

      // Seed an ACTIVE target so the menu offers "Deactivate".
      const entry = makeSummaryEntry('t-1', 'Senior Frontend Engineer', {
        activation_status: 'ready',
        fit_score: 80,
      });
      entry.user_target.is_active = true;
      render(<TargetsList initialTargets={[entry]} />);

      expect(screen.getByText(/^Active$/i)).toBeInTheDocument();

      await clickMenuAction(/^Deactivate$/i);

      // After the failed POST the badge rolls back to Active and an error
      // toast fires.
      await waitFor(() => {
        expect(mockToast).toHaveBeenCalledWith(
          expect.objectContaining({ variant: 'error' })
        );
      });
      expect(screen.getByText(/^Active$/i)).toBeInTheDocument();
      expect(mockRefresh).not.toHaveBeenCalled();
    });
  });

  describe('delete', () => {
    const originalFetch = global.fetch;
    afterEach(() => {
      global.fetch = originalFetch;
      mockToast.mockClear();
      mockRefresh.mockClear();
    });

    async function openDeleteDialog() {
      const user = userEvent.setup();
      render(
        <TargetsList
          initialTargets={[
            makeSummaryEntry('t-1', 'Senior Frontend Engineer', {
              fit_score: 80,
            }),
          ]}
        />
      );
      const trigger = document.querySelector(
        '[aria-haspopup="menu"]'
      ) as HTMLElement;
      await user.click(trigger);
      await user.click(screen.getByRole('menuitem', { name: /delete/i }));
      return user;
    }

    it('opens a confirm dialog from the Delete menu item without deleting', async () => {
      const fetchMock = jest.fn();
      global.fetch = fetchMock as unknown as typeof fetch;

      const user = await openDeleteDialog();
      const dialog = await screen.findByRole('dialog');
      expect(fetchMock).not.toHaveBeenCalled();

      await user.click(within(dialog).getByRole('button', { name: /cancel/i }));
      expect(fetchMock).not.toHaveBeenCalled();
      expect(screen.getByText('Senior Frontend Engineer')).toBeInTheDocument();
    });

    it('DELETEs the target and removes the card after confirming', async () => {
      const fetchMock = jest.fn().mockResolvedValue({ ok: true } as Response);
      global.fetch = fetchMock as unknown as typeof fetch;

      const user = await openDeleteDialog();
      const dialog = await screen.findByRole('dialog');
      await user.click(
        within(dialog).getByRole('button', { name: /^delete$/i })
      );

      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledWith('/api/targets/t-1', {
          method: 'DELETE',
        });
      });
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: 'success' })
      );
      await waitFor(() => {
        expect(screen.queryByText('Senior Frontend Engineer')).toBeNull();
      });
      expect(mockRefresh).toHaveBeenCalled();
    });

    it('toasts an error and keeps the card when delete fails', async () => {
      const fetchMock = jest.fn().mockResolvedValue({ ok: false } as Response);
      global.fetch = fetchMock as unknown as typeof fetch;

      const user = await openDeleteDialog();
      const dialog = await screen.findByRole('dialog');
      await user.click(
        within(dialog).getByRole('button', { name: /^delete$/i })
      );

      await waitFor(() => {
        expect(mockToast).toHaveBeenCalledWith(
          expect.objectContaining({ variant: 'error' })
        );
      });
      expect(screen.getByText('Senior Frontend Engineer')).toBeInTheDocument();
    });
  });
});
