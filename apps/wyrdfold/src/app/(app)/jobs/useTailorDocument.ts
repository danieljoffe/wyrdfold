'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { extractApiError } from '@/lib/extractApiError';
import { promptForMissingContactName } from './promptForMissingContactName';
import type {
  TailoredDocumentState,
  TailoredResumeRecord,
  TailorRunStatus,
} from './types';

/**
 * Client half of the non-blocking tailor flow (#656).
 *
 * `POST /api/jobs/tailor/{resume,cover-letter}` returns `202 {status:"running"}`
 * and the LLM pipeline runs server-side in a detached task, so this hook owns
 * the poll loop against `GET /api/jobs/tailor/by-job/{id}[/cover-letter]`.
 *
 * The two behaviors worth naming, because they're the reason the hook exists
 * rather than a `generating` boolean per component:
 *
 * 1. **It survives navigation.** The mount fetch reads `status` as well as the
 *    record, so a page opened while a generation is already in flight picks the
 *    poll straight back up — no POST, no double spend. The work persists
 *    server-side regardless of whether any client is watching.
 * 2. **It waits for a NEW record, not just any record.** Re-adapt (`force_fresh`)
 *    regenerates over a posting that already has a document, and the poll route
 *    returns the most recent one — so "a record exists" can't mean "done".
 *    Every wait is anchored to the record id observed before the kick.
 */

/** LLM pipelines here run ~27–39s (up to three calls for a resume), so ~2.5s
 *  polls resolve in ~10–16 round-trips. The ceiling is generous because the
 *  server keeps going past it: hitting it is a "check back later" message, not
 *  a cancellation, and a reload re-enters the loop via the mount fetch. */
const POLL_INTERVAL_MS = 2500;
const MAX_POLLS = 96; // ~4 minutes

const delay = (ms: number) =>
  new Promise<void>(resolve => setTimeout(resolve, ms));

export type TailorKind = 'resume' | 'cover_letter';

interface UseTailorDocumentOptions {
  jobPostingId: string;
  kind: TailorKind;
  onSettled?: (record: TailoredResumeRecord) => void;
}

export interface UseTailorDocumentResult {
  record: TailoredResumeRecord | null;
  /** True until the first state fetch resolves. */
  loading: boolean;
  /** True while a generation is in flight — including one started before this
   *  component mounted, or in another tab. */
  generating: boolean;
  /** Failure text from a background run (or a kick-off), else null. */
  error: string | null;
  /** Kick off a generation and wait for the resulting record. Returns true on
   *  success. `body` supplies the document-specific POST fields. */
  generate: (body: Record<string, unknown>) => Promise<boolean>;
  /** Re-read the current state (no LLM). */
  refresh: () => Promise<void>;
  setRecord: (record: TailoredResumeRecord) => void;
}

function pollUrl(jobPostingId: string, kind: TailorKind): string {
  const base = `/api/jobs/tailor/by-job/${jobPostingId}`;
  return kind === 'cover_letter' ? `${base}/cover-letter` : base;
}

function postUrl(kind: TailorKind): string {
  return kind === 'cover_letter'
    ? '/api/jobs/tailor/cover-letter'
    : '/api/jobs/tailor/resume';
}

const FAILED_LABEL: Record<TailorKind, string> = {
  resume: 'Resume generation failed',
  cover_letter: 'Cover letter generation failed',
};

export function useTailorDocument({
  jobPostingId,
  kind,
  onSettled,
}: UseTailorDocumentOptions): UseTailorDocumentResult {
  const [record, setRecordState] = useState<TailoredResumeRecord | null>(null);
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Identity token for the in-flight poll loop, mirroring JobDetailPanel's
  // #459 pattern. Each kick (and the mount fetch) claims a fresh token; the
  // loop bails the moment `activeRunRef` stops pointing at its own. That's
  // what lets the user navigate away mid-generation without a dangling loop
  // calling setState on an unmounted component — the server keeps running and
  // persists, so coming back finds the finished draft.
  const activeRunRef = useRef<object | null>(null);
  useEffect(
    () => () => {
      activeRunRef.current = null;
    },
    []
  );

  // `onSettled` is a caller-supplied callback that is often an inline arrow;
  // holding it in a ref keeps it out of the effect deps so a parent re-render
  // can't restart the mount fetch (and re-enter the poll loop) on every pass.
  const onSettledRef = useRef(onSettled);
  useEffect(() => {
    onSettledRef.current = onSettled;
  });

  const applyRecord = useCallback((next: TailoredResumeRecord) => {
    setRecordState(next);
    onSettledRef.current?.(next);
  }, []);

  const fetchState =
    useCallback(async (): Promise<TailoredDocumentState | null> => {
      const res = await fetch(pollUrl(jobPostingId, kind));
      if (!res.ok) return null;
      return (await res.json()) as TailoredDocumentState;
    }, [jobPostingId, kind]);

  /**
   * Poll until a record NEWER than `previousId` lands (or the run errors).
   * Anchoring on the id is what makes re-adapt work: the route always returns
   * the most recent document, so "a record exists" is true from the first tick.
   */
  const waitForNewRecord = useCallback(
    async (
      previousId: string | null,
      alive: () => boolean
    ): Promise<boolean> => {
      for (let attempt = 0; attempt < MAX_POLLS; attempt += 1) {
        await delay(POLL_INTERVAL_MS);
        if (!alive()) return false;

        const state = await fetchState();
        if (!alive()) return false;
        if (state === null) {
          setError(FAILED_LABEL[kind]);
          return false;
        }

        if (state.record && state.record.id !== previousId) {
          applyRecord(state.record);
          return true;
        }
        if (state.status === 'error') {
          setError(state.message ?? `${FAILED_LABEL[kind]}. Please retry.`);
          return false;
        }
        if (state.status === 'idle') {
          // Nothing in flight and no new document: the server dropped the run
          // (a deploy restarted the API mid-generation). Re-kicking here would
          // silently spend again, so hand back to an explicit retry.
          setError('Generation was interrupted. Please retry.');
          return false;
        }
        // `running` → keep polling.
      }
      setError('Still generating — check back in a moment.');
      return false;
    },
    [fetchState, applyRecord, kind]
  );

  const refresh = useCallback(async () => {
    const state = await fetchState();
    if (state?.record) setRecordState(state.record);
  }, [fetchState]);

  // Mount: read the current state, and resume polling if a generation started
  // elsewhere (another tab, or this one before a navigation) is still running.
  useEffect(() => {
    const token = {};
    activeRunRef.current = token;
    const alive = () => activeRunRef.current === token;

    (async () => {
      let state: TailoredDocumentState | null = null;
      setLoading(true);
      try {
        state = await fetchState();
      } catch {
        // Non-critical on initial load — the Generate CTA still renders.
      } finally {
        // Cleared as soon as the STATE read resolves, deliberately before the
        // poll loop below. Holding `loading` for the loop's duration would
        // park an adopted run on the "checking for a draft…" placeholder
        // instead of the "Generating…" state it's actually in.
        if (alive()) setLoading(false);
      }
      if (!alive() || state === null) return;

      setRecordState(state.record);
      if (state.status === 'error' && !state.record) {
        setError(state.message ?? `${FAILED_LABEL[kind]}. Please retry.`);
        return;
      }
      if (state.status !== 'running') return;

      // A run we didn't start is in flight — adopt it rather than kicking a
      // second one. `state.record` is the pre-run document (null on a first
      // generation), which is exactly the anchor the wait needs.
      setGenerating(true);
      try {
        await waitForNewRecord(state.record?.id ?? null, alive);
      } catch {
        setError(`${FAILED_LABEL[kind]}. Please retry.`);
      } finally {
        if (alive()) setGenerating(false);
      }
    })();
  }, [fetchState, waitForNewRecord, kind]);

  const generate = useCallback(
    async (body: Record<string, unknown>): Promise<boolean> => {
      const token = {};
      activeRunRef.current = token;
      const alive = () => activeRunRef.current === token;

      const previousId = record?.id ?? null;
      setGenerating(true);
      setError(null);
      try {
        const post = () =>
          fetch(postUrl(kind), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          });

        let res = await post();

        // Contact-name gate. Still a real status on the POST — contact
        // resolution deliberately runs in front of the 202 precisely so this
        // inline prompt-and-retry keeps working. See the API router docstring.
        if (!res.ok) {
          const peek = (await res
            .clone()
            .json()
            .catch(() => null)) as {
            detail?: { code?: string; message?: string } | string;
          } | null;
          const peekDetail =
            typeof peek?.detail === 'string' ? peek.detail : undefined;
          if (await promptForMissingContactName(peekDetail)) {
            res = await post();
          }
        }

        if (!res.ok) {
          // `gap_gate` is a structured 422 carrying its own message; peek for
          // it before falling back to extractApiError (which handles plain
          // detail strings and the structured 429s but would render this one
          // as a status-prefixed generic). Order matters.
          const peek = (await res
            .clone()
            .json()
            .catch(() => null)) as {
            detail?: { code?: string; message?: string } | string;
          } | null;
          const peekDetail = peek?.detail;
          if (
            typeof peekDetail === 'object' &&
            peekDetail !== null &&
            (peekDetail.code === 'gap_gate' ||
              peekDetail.code === 'tailor_concurrent_limit')
          ) {
            setError(
              peekDetail.message ?? 'Master doc has gaps — update it first'
            );
          } else {
            setError(await extractApiError(res, FAILED_LABEL[kind]));
          }
          return false;
        }

        // 200 = the record came back inline: a reuse clone (#504, no LLM) or
        // the JD-only path. 202 = backgrounded; poll for it.
        if (res.status !== 202) {
          const data = (await res.json()) as {
            record?: TailoredResumeRecord;
          };
          if (!alive()) return false;
          if (data.record) {
            applyRecord(data.record);
            return true;
          }
        }

        return await waitForNewRecord(previousId, alive);
      } catch {
        if (alive()) setError('Network error generating document.');
        return false;
      } finally {
        if (alive()) setGenerating(false);
      }
    },
    [kind, record, applyRecord, waitForNewRecord]
  );

  return {
    record,
    loading,
    generating,
    error,
    generate,
    refresh,
    setRecord: setRecordState,
  };
}

export type { TailorRunStatus };
