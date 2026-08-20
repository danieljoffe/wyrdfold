'use client';

import { useEffect, useState } from 'react';

/**
 * Advance through a list of status messages while ``active`` is true.
 *
 * The onboarding wizard's long LLM-backed waits (resume parse ~45s,
 * target suggestions ~20s, observed prod 2026-08-13) showed a single
 * static line the whole time, which reads as "hung" long before the work
 * fails. Returns ``messages[0]`` immediately and steps forward every
 * ``intervalMs`` until the last message, which then holds. Resets to the
 * first message whenever ``active`` flips off.
 */
export function useStagedMessage(
  messages: readonly string[],
  active: boolean,
  intervalMs = 8000
): string {
  const [index, setIndex] = useState(0);

  useEffect(() => {
    if (!active) {
      setIndex(0);
      return;
    }
    if (messages.length < 2) return;
    const timer = setInterval(() => {
      setIndex(prev => Math.min(prev + 1, messages.length - 1));
    }, intervalMs);
    return () => clearInterval(timer);
  }, [active, intervalMs, messages]);

  return messages[Math.min(index, messages.length - 1)] ?? '';
}
