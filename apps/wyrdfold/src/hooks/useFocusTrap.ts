import { useEffect, useRef } from 'react';

const FOCUSABLE_SELECTOR =
  'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';

/**
 * Traps Tab focus within a container while `isActive`, focuses the first
 * focusable element on open, and **restores focus to the trigger on close**.
 *
 * #196: restoration previously read a `[data-previously-focused]` attribute
 * that nothing in the app ever set, and only ran on the hook's own Escape
 * handler — so closing a dialog (Escape-to-close lives in the consumer, plus
 * backdrop / button / unmount) dropped focus to `<body>` (WCAG 2.4.3). We now
 * capture `document.activeElement` when the trap opens and restore it from the
 * effect cleanup, which runs on every close path. Tab targets are re-queried on
 * each keypress so focusable elements added after open are covered too.
 */
export function useFocusTrap(isActive: boolean) {
  const containerRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!isActive || !containerRef.current) return;

    const container = containerRef.current;
    // The element focused before the trap opened — restored on cleanup.
    const previouslyFocused = document.activeElement as HTMLElement | null;

    // Re-query on every Tab so focusables added after open (async modal
    // content) are included — the list used to be captured once at open.
    const getFocusable = () =>
      Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));

    const handleTabKey = (e: KeyboardEvent) => {
      if (e.key !== 'Tab') return;
      const focusable = getFocusable();
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];

      if (e.shiftKey) {
        if (document.activeElement === first) {
          e.preventDefault();
          last.focus();
        }
      } else if (document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };

    getFocusable()[0]?.focus();
    document.addEventListener('keydown', handleTabKey);

    return () => {
      document.removeEventListener('keydown', handleTabKey);
      // Return focus to the trigger so keyboard/AT users aren't dumped at the
      // top of the document. Guard: it may have been removed while open.
      if (
        previouslyFocused &&
        previouslyFocused !== document.body &&
        typeof previouslyFocused.focus === 'function' &&
        document.contains(previouslyFocused)
      ) {
        previouslyFocused.focus();
      }
    };
  }, [isActive]);

  return containerRef;
}
