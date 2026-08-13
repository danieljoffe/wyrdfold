import '@testing-library/jest-dom';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { AddToTargetMenu } from '../JobSearchExplorer';
import type { TargetsSource } from '../useTargetsSource';

jest.mock('@/state/Toast/ToastProvider', () => ({
  useToast: () => ({ toast: jest.fn() }),
}));
jest.mock('@/app/(app)/targets/targetFlows', () => ({
  addJobToTarget: jest.fn().mockResolvedValue(undefined),
}));
jest.mock('next/navigation', () => ({
  usePathname: () => '/search',
  useRouter: () => ({ push: jest.fn(), replace: jest.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

function makeSource(): TargetsSource {
  return {
    targets: [{ id: 't-1', label: 'My target', isActive: true }],
    loading: false,
    error: null,
    ensureLoaded: jest.fn(),
  } as unknown as TargetsSource;
}

/**
 * #485 nested-dismiss guard: shared-ui Modal listens for Escape on window,
 * and the Dropdown's Escape handler doesn't stop propagation. The menu's
 * wrapper must swallow exactly the Escapes the menu CONSUMED
 * (``defaultPrevented`` — the primitive preventDefault()s before closing),
 * so one keystroke closes the menu, and only the NEXT one reaches a
 * hosting modal. Gating on component open-state instead is a real race we
 * shipped and reverted: keydown is a discrete event, so the close's
 * setState flushes synchronously mid-dispatch.
 */
describe('AddToTargetMenu Escape nesting (#485)', () => {
  it('swallows the Escape that closed the menu; passes the next one through', async () => {
    const windowSawEscape = jest.fn();
    window.addEventListener('keydown', windowSawEscape);
    try {
      render(<AddToTargetMenu jobId='job-1' source={makeSource()} />);
      const trigger = screen.getByRole('button', { name: /add to target/i });

      fireEvent.click(trigger);
      await waitFor(() => expect(screen.getByRole('menu')).toBeInTheDocument());

      // Escape #1: the menu consumes it (primitive preventDefault()s) and
      // the guard stops propagation — window must NOT see it.
      fireEvent.keyDown(trigger, {
        key: 'Escape',
        bubbles: true,
        cancelable: true,
      });
      await waitFor(() =>
        expect(screen.queryByRole('menu')).not.toBeInTheDocument()
      );
      expect(windowSawEscape).not.toHaveBeenCalled();

      // Escape #2 (menu closed): unconsumed, so it propagates to window —
      // a hosting modal is allowed to close now.
      fireEvent.keyDown(trigger, {
        key: 'Escape',
        bubbles: true,
        cancelable: true,
      });
      expect(windowSawEscape).toHaveBeenCalledTimes(1);
    } finally {
      window.removeEventListener('keydown', windowSawEscape);
    }
  });
});

/** §B2 (ux-sweep 2026-08-12): the picker listed every target with no
 * active/inactive distinction while Jobs shows only active tabs. */
describe('AddToTargetMenu inactive labeling (§B2)', () => {
  it('marks inactive targets and leaves active ones bare', async () => {
    const source = {
      targets: [
        { id: 't-1', label: 'Active target', isActive: true },
        { id: 't-2', label: 'Paused target', isActive: false },
      ],
      loading: false,
      error: null,
      ensureLoaded: jest.fn(),
    } as unknown as TargetsSource;

    render(<AddToTargetMenu jobId='job-1' source={source} />);
    fireEvent.click(screen.getByRole('button', { name: /add to target/i }));
    await waitFor(() => expect(screen.getByRole('menu')).toBeInTheDocument());

    expect(screen.getByText('Paused target')).toBeInTheDocument();
    expect(screen.getByText('inactive')).toBeInTheDocument();
    // Exactly one hint — the active target carries none.
    expect(screen.getAllByText('inactive')).toHaveLength(1);
    // Every item carries its full label as a native tooltip — truncated
    // labels were unreadable in the fixed-width menu (re-sweep R6).
    expect(screen.getByTitle('Active target')).toBeInTheDocument();
    expect(screen.getByTitle('Paused target')).toBeInTheDocument();
  });
});
