import React from 'react';
import '@testing-library/jest-dom';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ProfilePage from '../ProfilePage';

const mockToast = jest.fn();
jest.mock('@/state/Toast/ToastProvider', () => ({
  useToast: () => ({ toast: mockToast }),
}));

jest.mock('next/navigation', () => ({
  useRouter: () => ({ push: jest.fn(), prefetch: jest.fn() }),
}));

// The conversation chat modal is gated by chatOpen and pulls in Anthropic /
// SSE plumbing. Stub it to keep the spec focused on the page shell.
jest.mock('@/app/_components/ConversationChatModal', () => ({
  __esModule: true,
  default: () => null,
}));

const originalFetch = global.fetch;

function jsonRes(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

beforeEach(() => {
  mockToast.mockReset();
});

afterEach(() => {
  global.fetch = originalFetch;
});

describe('ProfilePage', () => {
  it('renders the upload-resume zero state when no profile data exists', async () => {
    global.fetch = jest.fn().mockImplementation((url: string) => {
      if (url.includes('/optimized'))
        return Promise.resolve(jsonRes({ optimized: null }));
      if (url.includes('/gap-health'))
        return Promise.resolve(jsonRes({ tier: 'red', gaps: [] }));
      if (url.includes('/prose'))
        return Promise.resolve(jsonRes({ prose: null }));
      return Promise.resolve(jsonRes({}, 404));
    }) as unknown as typeof fetch;

    render(<ProfilePage />);

    expect(await screen.findByText(/Upload your resume/i)).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /Upload Resume/i })
    ).toBeInTheDocument();
  });

  it('toasts an error when the initial fetch rejects', async () => {
    global.fetch = jest
      .fn()
      .mockRejectedValue(new Error('network')) as unknown as typeof fetch;

    render(<ProfilePage />);

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: 'error' })
      );
    });
  });

  it('debounced prose autosave POSTs prose then refreshes only gap-health (no redundant prose/optimized re-fetch)', async () => {
    const fetchMock = jest.fn().mockImplementation((url: string) => {
      if (url.includes('/optimized'))
        return Promise.resolve(jsonRes({ optimized: null }));
      if (url.includes('/gap-health'))
        return Promise.resolve(
          jsonRes({ tier: 'green', gap_pct: 0, gaps: [] })
        );
      if (url.includes('/prose'))
        return Promise.resolve(
          jsonRes({
            id: 'prose-1',
            user_id: null,
            version: 1,
            content: 'Initial',
            created_at: '2026-06-18',
          })
        );
      return Promise.resolve(jsonRes({}, 404));
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<ProfilePage />);

    // Wait for the initial load (3 GETs) to settle and the editor to mount.
    // Use real timers here so findByRole's polling can resolve the fetch
    // microtasks; switch to fake timers afterwards to drive the debounce.
    const textarea = await screen.findByRole('textbox', {
      name: /master document/i,
    });

    // Initial load fires exactly one GET to each endpoint.
    const countGets = (fragment: string) =>
      fetchMock.mock.calls.filter(
        ([url, init]) =>
          typeof url === 'string' &&
          url.includes(fragment) &&
          (init?.method ?? 'GET') === 'GET'
      ).length;
    expect(countGets('/prose')).toBe(1);
    expect(countGets('/optimized')).toBe(1);
    expect(countGets('/gap-health')).toBe(1);

    jest.useFakeTimers();
    const user = userEvent.setup({
      advanceTimers: (ms: number) => jest.advanceTimersByTime(ms),
    });
    await user.type(textarea, ' more');

    // Cross the 800ms debounce so the autosave fires.
    await act(async () => {
      jest.advanceTimersByTime(800);
    });

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(
          ([url, init]) =>
            typeof url === 'string' &&
            url.includes('/prose') &&
            init?.method === 'POST'
        )
      ).toBe(true);
    });

    // After save: gap-health refreshed (2nd GET), but prose/optimized are NOT
    // re-fetched — the local draft is authoritative and the LLM re-derive stays
    // behind the explicit Re-derive button.
    await waitFor(() => {
      expect(countGets('/gap-health')).toBe(2);
    });
    expect(countGets('/prose')).toBe(1);
    expect(countGets('/optimized')).toBe(1);

    jest.useRealTimers();
  });

  describe('delete master document', () => {
    function mockLoadedWithProse() {
      const fetchMock = jest.fn().mockImplementation((url: string) => {
        if (url.includes('/optimized'))
          return Promise.resolve(jsonRes({ optimized: null }));
        if (url.includes('/gap-health'))
          return Promise.resolve(
            jsonRes({ tier: 'green', gap_pct: 0, gaps: [] })
          );
        if (url.includes('/prose'))
          return Promise.resolve(
            jsonRes({
              id: 'prose-1',
              user_id: null,
              version: 1,
              content: 'Initial',
              created_at: '2026-06-18',
            })
          );
        return Promise.resolve(jsonRes({}, 404));
      });
      global.fetch = fetchMock as unknown as typeof fetch;
      return fetchMock;
    }

    it('opens a confirm dialog from the Delete button without deleting', async () => {
      const fetchMock = mockLoadedWithProse();
      const user = userEvent.setup();
      render(<ProfilePage />);

      await screen.findByRole('textbox', { name: /master document/i });
      await user.click(screen.getByRole('button', { name: /^delete$/i }));

      const dialog = await screen.findByRole('dialog');
      expect(
        fetchMock.mock.calls.some(([, init]) => init?.method === 'DELETE')
      ).toBe(false);

      // Cancelling closes the dialog without deleting.
      await user.click(within(dialog).getByRole('button', { name: /cancel/i }));
      expect(
        fetchMock.mock.calls.some(([, init]) => init?.method === 'DELETE')
      ).toBe(false);
    });

    it('DELETEs the master document after confirming', async () => {
      const fetchMock = mockLoadedWithProse();
      const user = userEvent.setup();
      render(<ProfilePage />);

      await screen.findByRole('textbox', { name: /master document/i });
      await user.click(screen.getByRole('button', { name: /^delete$/i }));

      const dialog = await screen.findByRole('dialog');
      await user.click(
        within(dialog).getByRole('button', { name: /^delete$/i })
      );

      await waitFor(() => {
        expect(
          fetchMock.mock.calls.some(
            ([url, init]) =>
              typeof url === 'string' &&
              url.includes('/prose') &&
              init?.method === 'DELETE'
          )
        ).toBe(true);
      });
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: 'success' })
      );
    });
  });
});
