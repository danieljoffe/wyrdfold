'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Heading } from '@danieljoffe/shared-ui/Heading';
import { Spinner } from '@danieljoffe/shared-ui/Spinner';
import { Text } from '@danieljoffe/shared-ui/Text';
import { Card, CardContent } from '@danieljoffe/shared-ui/Card';
import LinkButton from '@/components/kit/LinkButton';
import ConfirmModal from '@/components/ConfirmModal';
import { extractApiError } from '@/lib/extractApiError';
import { useToast } from '@/state/Toast/ToastProvider';
import { useBatchStatus } from './useBatchStatus';
import { useJobRemove } from './useJobRemove';
import { cn } from '@/lib/cn';
import BatchActionBar from './BatchActionBar';
import {
  filtersToUrlPatch,
  isFilterStateEmpty,
  pickFilters,
} from './jobsFilterFields';
import JobsListView from './JobsListView';
import JobsThinResultsCallout from './JobsThinResultsCallout';
import { promptForMissingContactName } from './promptForMissingContactName';
import type {
  JobPosting,
  JobsFilterState,
  JobsSortColumn,
  JobStatus,
} from './types';
import { useJobsFilterPersistence } from './useJobsFilterPersistence';
import { useJobsUrlState } from './useJobsUrlState';

export interface TargetTab {
  id: string;
  label: string;
}

const BATCH_POLL_INTERVAL = 3000;

/**
 * Budget for the target-activation status poll: 10 ticks at 3s (fresh
 * feedback while a normal derive/poll finishes) then 15s per tick — the
 * same 60-attempt cap now covers ~13 minutes instead of burning out in 3
 * at 20 req/min (#851 P4; re-activation pipelines that re-poll sources
 * can legitimately run for minutes, and prod logs showed the fast cadence
 * sustained across those windows). On cap reached we clear the timer and
 * rely on the next tab switch to pick up the eventually-settled state.
 * Hidden tabs don't tick at all — the
 * visibilitychange listener resumes the loop, so a backgrounded /jobs tab
 * stops contributing request noise entirely.
 */
const STATUS_POLL_MAX_ATTEMPTS = 60;
const STATUS_POLL_FAST_ATTEMPTS = 10;
const STATUS_POLL_FAST_MS = 3_000;
const STATUS_POLL_SLOW_MS = 15_000;

/** Last-picked jobs tab ('' = All Jobs) — replayed on bare entry (#607). */
const LAST_TAB_STORAGE_KEY = 'wyrdfold.jobs.lastTab';

interface JobsListProps {
  targetId: string | undefined;
  initialStatus?: string;
  initialMinScore?: string;
  initialTargets: TargetTab[];
}

export default function JobsList({
  targetId,
  initialStatus,
  initialMinScore,
  initialTargets,
}: JobsListProps) {
  // Single source of truth for filters / sort / page / target — backed by
  // URL query params so browser back/forward restores every dimension of
  // the page state. The ``initialStatus`` and ``initialMinScore`` props
  // were the previous server-side prelude to this state; they're now
  // strictly fallbacks for the (rare) case where the URL has no params
  // (e.g. server-side prerender before client hydration).
  // Default to All Jobs (undefined targetId) when no `?target=X` is in the
  // URL. Previously this fell through to ``initialTargets[0]`` which made
  // the first active target sticky as the "default tab"; users with
  // multiple targets kept landing on a per-target view they didn't pick.
  const defaultTargetId = targetId;
  const { state: urlState, setState: setUrlState } = useJobsUrlState({
    defaultSort: 'score',
    defaultOrder: 'desc',
    defaultTargetId,
  });

  // Derived filter view for the existing JobsFilter component — keeps that
  // component oblivious to the URL plumbing. Falls back to the server-side
  // ``initialStatus`` / ``initialMinScore`` props only when the URL has no
  // value for that key, so a deep-linked ``/jobs?status=applied`` URL still
  // pins the filter on the first client render.
  const filters: JobsFilterState = useMemo(
    () => ({
      search: urlState.search,
      status: urlState.status || initialStatus || '',
      minScore: urlState.minScore || initialMinScore || '',
      excludeLocations: urlState.excludeLocations,
      onlyLocations: urlState.onlyLocations,
      remoteOnly: urlState.remoteOnly,
      minSalary: urlState.minSalary,
      country: urlState.country,
    }),
    [
      urlState.search,
      urlState.status,
      urlState.minScore,
      urlState.excludeLocations,
      urlState.onlyLocations,
      urlState.remoteOnly,
      urlState.minSalary,
      urlState.country,
      initialStatus,
      initialMinScore,
    ]
  );

  const setFilters = useCallback(
    (next: JobsFilterState) => {
      // Empty string → null so inactive dimensions leave the URL entirely.
      // No page reset needed: ``useAdminTableFetch`` re-fetches the first
      // page (and drops the accumulated list) whenever filters change.
      setUrlState(filtersToUrlPatch(next));
    },
    [setUrlState]
  );

  const activeTargetId = urlState.targetId;

  // Last-picked tab memory (#607). A bare sidebar entry to /jobs restores
  // whichever tab the user last chose — including "All Jobs" (stored as
  // ''). This deliberately does NOT resurrect the removed first-active-
  // target default above: only an explicit prior choice is replayed, and
  // only when the URL carries no target of its own (deep links win).
  const restoredLastTabRef = useRef(false);
  useEffect(() => {
    if (restoredLastTabRef.current) return;
    restoredLastTabRef.current = true;
    if (urlState.targetId !== undefined) return;
    let stored: string | null = null;
    try {
      stored = localStorage.getItem(LAST_TAB_STORAGE_KEY);
    } catch {
      return;
    }
    if (!stored) return;
    if (!targets.some(t => t.id === stored)) return;
    setUrlState({ targetId: stored });
    // Mount-only restore by design — later target changes are user choices.
  }, []);

  // Record every explicit tab choice once the restore pass has run (the
  // guard keeps the pre-restore undefined from clobbering the stored id).
  useEffect(() => {
    if (!restoredLastTabRef.current) return;
    try {
      localStorage.setItem(LAST_TAB_STORAGE_KEY, activeTargetId ?? '');
    } catch {
      // Storage unavailable (private mode) — memory is best-effort.
    }
  }, [activeTargetId]);

  // Per-target filter persistence. localStorage-backed; survives reloads
  // and out-of-page navigation but not browser-data clears. Writes happen
  // on every filter change (below). Reads happen on tab change + on first
  // mount when the URL has no filter params (just below). See
  // ``useJobsFilterPersistence`` for the storage key scheme.
  const persistence = useJobsFilterPersistence();

  // Track whether we've attempted a restore for the current target so a
  // user-triggered "clear all filters" doesn't immediately re-apply the
  // saved snapshot on the next render.
  const restoredForTargetRef = useRef<string | null | undefined>(null);

  // The URL state narrowed to just the filter dimensions (drops
  // sort/order/target navigation fields). ``urlState`` is memoised on the
  // search params, so this recomputes only when the URL actually changes.
  const urlFilters = useMemo(() => pickFilters(urlState), [urlState]);

  // Snapshot to localStorage whenever the live filters change. Writes
  // are keyed by the current target (or the All Jobs sentinel) so each
  // target remembers its own filter state independently.
  //
  // Gated on the restore pass having run for this target: this effect is
  // declared first, so on a BARE mount it would otherwise fire with the
  // empty URL and delete the very snapshot the restore effect (below) is
  // about to read — which silently broke restore-on-re-entry. After the
  // restore pass, writes flow normally, including the deliberate
  // "cleared all filters" removeItem.
  useEffect(() => {
    if (restoredForTargetRef.current !== activeTargetId) return;
    persistence.write(activeTargetId, urlFilters);
  }, [persistence, activeTargetId, urlFilters]);

  // Restore from localStorage on first mount per target if the URL has
  // no filter params. Deep links (``/jobs?q=react``) win over the
  // stored snapshot — the URL is always authoritative when populated.
  useEffect(() => {
    if (restoredForTargetRef.current === activeTargetId) return;
    restoredForTargetRef.current = activeTargetId;

    if (!isFilterStateEmpty(urlFilters)) return;

    const saved = persistence.read(activeTargetId);
    if (!saved) return;

    setUrlState(filtersToUrlPatch(saved));
    // Intentionally narrow deps: we only want this to fire on the first
    // render per target. Including ``urlState`` would re-trigger after
    // the restore writes its own values back into the URL, which is
    // already guarded by the ref check above but reads more clearly
    // with a small dep array.
  }, [activeTargetId, persistence, setUrlState]);

  // Sort/order wiring for ``useAdminTableFetch``. Defined here so we can
  // hand them down to JobsListView as a controlled pair + change callback.
  // Sort defaults are mirrored from ``useJobsUrlState`` so the values match.
  // (Pagination isn't URL-backed — it's an in-memory load-more cursor.)
  const controlledTableState = useMemo(
    () => ({
      sort: urlState.sort as JobsSortColumn,
      order: urlState.order,
    }),
    [urlState.sort, urlState.order]
  );

  const onTableSortChange = useCallback(
    (sort: JobsSortColumn, order: 'asc' | 'desc', meta?: { reset: boolean }) => {
      // Sort changes create a history entry so back restores the old sort.
      // The hook re-fetches the first page when sort changes.
      //
      // A CLEARED sort drops the params entirely rather than pinning the
      // default values. Writing `sort=score&order=desc` would look identical
      // to the user but leaves a URL that survives a change of default — and
      // "cleared" should mean "whatever the app ranks by", not "score, frozen".
      setUrlState(meta?.reset ? { sort: null, order: null } : { sort, order }, 'push');
    },
    [setUrlState]
  );

  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [refreshKey, setRefreshKey] = useState(0);
  const [generating, setGenerating] = useState(false);
  const [batchProgress, setBatchProgress] = useState<
    { completed: number; total: number } | undefined
  >(undefined);
  const [exporting, setExporting] = useState(false);
  const [confirmBatchDeleteOpen, setConfirmBatchDeleteOpen] = useState(false);
  const { removeJobs, removing: batchDeleting } = useJobRemove();
  const { setStatusForJobs, updating: statusUpdating } = useBatchStatus();
  const [visiblePostings, setVisiblePostings] = useState<JobPosting[]>([]);
  const [activationStatus, setActivationStatus] = useState<string>('idle');
  // Total job count for the active target, sourced from
  // ``/api/targets/{id}/status``. Drives the thin-results CTA. Reset
  // on tab switch so a fresh target doesn't briefly show the previous
  // target's count.
  const [jobsCount, setJobsCount] = useState<number | null>(null);
  const activatingRef = useRef<Set<string>>(new Set());
  const pollRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const { toast } = useToast();

  const targets = initialTargets;
  /** Label of the tab in scope; undefined on All Jobs (no single target). */
  const activeTargetLabel = targets.find(t => t.id === activeTargetId)?.label;

  // Check target activation status when switching tabs
  useEffect(() => {
    if (!activeTargetId) return;

    let cancelled = false;
    let attempts = 0;
    // True between ensureStatusPoll() and clearStatusPoll() — i.e. "the
    // pipeline is mid-flight and we owe the user updates". Lets the
    // visibilitychange handler distinguish "paused while hidden" from
    // "settled, nothing to resume".
    let polling = false;
    const statusPollRef: {
      current: ReturnType<typeof setTimeout> | undefined;
    } = { current: undefined };

    function clearStatusPoll() {
      polling = false;
      if (statusPollRef.current) {
        clearTimeout(statusPollRef.current);
        // Null out the ref so a later branch that gates on
        // ``!statusPollRef.current`` can re-establish polling.
        // Previously the cleared timer ID lingered, so going
        // ready → deriving → ready never re-established polling
        // on the second cycle.
        statusPollRef.current = undefined;
      }
    }

    // Chained setTimeout rather than setInterval: each tick schedules the
    // next, so the cadence can back off after the fast window, and a hidden
    // tab schedules nothing until it's visible again.
    function scheduleTick() {
      if (statusPollRef.current) return;
      const delay =
        attempts < STATUS_POLL_FAST_ATTEMPTS
          ? STATUS_POLL_FAST_MS
          : STATUS_POLL_SLOW_MS;
      statusPollRef.current = setTimeout(() => {
        statusPollRef.current = undefined;
        // Hidden tab: park the chain — onVisibility re-enters it. The
        // attempt budget only counts requests actually issued.
        if (document.visibilityState === 'hidden') return;
        void checkStatus();
      }, delay);
    }

    function ensureStatusPoll() {
      polling = true;
      scheduleTick();
    }

    function onVisibility() {
      if (
        document.visibilityState === 'visible' &&
        polling &&
        !statusPollRef.current &&
        !cancelled
      ) {
        void checkStatus();
      }
    }
    document.addEventListener('visibilitychange', onVisibility);

    async function checkStatus() {
      // Bail before issuing the network request when we've exhausted
      // the budget. Without this a target genuinely stuck in `deriving`
      // would tick 20×/min indefinitely while the tab was open (#851 P4).
      attempts += 1;
      if (attempts > STATUS_POLL_MAX_ATTEMPTS) {
        clearStatusPoll();
        return;
      }
      try {
        const res = await fetch(`/api/targets/${activeTargetId}/status`);
        if (cancelled) return;
        if (!res.ok) {
          // Transient failure mid-pipeline: keep the chain alive (the old
          // setInterval retried implicitly; a chained timeout must
          // reschedule or a single 5xx freezes the status UI).
          if (polling) scheduleTick();
          return;
        }
        const data = (await res.json()) as {
          activation_status: string;
          jobs_count: number;
        };
        if (cancelled) return;

        setActivationStatus(data.activation_status);
        setJobsCount(data.jobs_count);

        if (data.activation_status === 'ready') {
          // Jobs are ready — refresh the table
          clearStatusPoll();
          setRefreshKey(k => k + 1);
        } else if (data.activation_status === 'idle') {
          // Only POST /activate once per session per target;
          // activatingRef tracks which ones we've already kicked off.
          // But always ensure polling is running — without this the
          // user could land on a target whose ref says "already
          // activated" but whose status hasn't progressed, and the
          // UI would silently get stuck on 'idle' forever.
          if (!activatingRef.current.has(activeTargetId!)) {
            activatingRef.current.add(activeTargetId!);
            await fetch(`/api/targets/${activeTargetId}/activate`, {
              method: 'POST',
            });
            // Navigation away during the POST shouldn't leave a
            // no-op poller attached to a cancelled effect.
            if (cancelled) return;
          }
          ensureStatusPoll();
        } else if (
          data.activation_status === 'deriving' ||
          data.activation_status === 'polling'
        ) {
          // Pipeline in progress — keep polling
          ensureStatusPoll();
        } else if (data.activation_status === 'error') {
          clearStatusPoll();
        }
      } catch {
        // Non-critical — reschedule if mid-pipeline, else the next tab
        // switch retries.
        if (!cancelled && polling) scheduleTick();
      }
    }

    checkStatus();

    return () => {
      cancelled = true;
      document.removeEventListener('visibilitychange', onVisibility);
      clearStatusPoll();
    };
  }, [activeTargetId]);

  const handleTabChange = useCallback(
    (id: string | undefined) => {
      setActivationStatus('idle');
      setJobsCount(null);
      setSelectedIds(new Set());
      // Tab change writes the new target with ``push`` so the back
      // button restores the previous tab. Filters carry the saved
      // snapshot for the destination target (each tab remembers its
      // own filters) — falls back to empty when there's nothing saved.
      const saved = persistence.read(id);
      setUrlState(
        {
          targetId: id ?? null,
          search: saved?.search || null,
          status: saved?.status || null,
          minScore: saved?.minScore || null,
          excludeLocations: saved?.excludeLocations || null,
          onlyLocations: saved?.onlyLocations || null,
        },
        'push'
      );
    },
    [persistence, setUrlState]
  );

  // Cleanup polling on unmount (chained setTimeout — see handleBatchGenerate).
  useEffect(() => {
    return () => {
      if (pollRef.current) clearTimeout(pollRef.current);
    };
  }, []);

  const handleBatchGenerate = useCallback(async () => {
    if (selectedIds.size === 0) return;

    setGenerating(true);
    try {
      const postBatch = () =>
        fetch('/api/jobs/tailor/batch', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            job_posting_ids: [...selectedIds],
          }),
        });

      let res = await postBatch();

      // The synchronous batch POST hits the same ``resolve_contact``
      // gate as the single-job tailor routes — a user who skipped
      // the onboarding identity step (#703) and clicks batch-generate
      // would otherwise dead-end on "No contact name on file".
      // Mirrors the inline-prompt + retry pattern from
      // ResumeSection / CoverLetterSection.
      if (!res.ok) {
        const peek = (await res
          .clone()
          .json()
          .catch(() => null)) as { detail?: unknown } | null;
        const detailString =
          typeof peek?.detail === 'string' ? peek.detail : undefined;
        if (await promptForMissingContactName(detailString)) {
          res = await postBatch();
        }
      }

      if (!res.ok) {
        // ``extractApiError`` understands the structured
        // ``llm_budget_exceeded`` 429 detail (PR #701) and plain-
        // string details (404 missing optimized doc, missing job).
        // The previous ad-hoc ``err.detail`` parser cast a possibly-
        // object detail to ``Record<string, string>``, so budget-
        // exceeded errors rendered as the generic fallback instead
        // of the actionable "$X of $Y, try again in an hour"
        // message.
        toast({
          variant: 'error',
          title: await extractApiError(res, 'Batch generation failed'),
        });
        setGenerating(false);
        return;
      }

      const { batch_id, total } = (await res.json()) as {
        batch_id: string;
        total: number;
      };
      setBatchProgress({ completed: 0, total });

      // Poll for completion. Chained setTimeout (no overlap): each fetch
      // schedules the next only after it resolves, so a slow upstream
      // can't fan out parallel in-flight requests that race state
      // updates (#851 P5). Same pattern as TargetsList.tsx:213-217.
      // ``pollRef`` now holds a setTimeout handle.
      if (pollRef.current) clearTimeout(pollRef.current);

      const pollOnce = async () => {
        try {
          const pollRes = await fetch(`/api/jobs/tailor/batch/${batch_id}`);
          if (!pollRes.ok) {
            pollRef.current = setTimeout(
              () => void pollOnce(),
              BATCH_POLL_INTERVAL
            );
            return;
          }

          const batch = (await pollRes.json()) as {
            status: string;
            completed: number;
            failed: number;
            total: number;
          };

          // F3-B: surface live progress in the action bar
          setBatchProgress({
            completed: batch.completed + batch.failed,
            total: batch.total,
          });

          if (batch.status === 'completed' || batch.status === 'failed') {
            pollRef.current = undefined;
            setGenerating(false);
            setBatchProgress(undefined);
            setSelectedIds(new Set());
            setRefreshKey(k => k + 1);

            if (batch.failed > 0) {
              toast({
                variant: 'warning',
                title: `Batch done: ${batch.completed} succeeded, ${batch.failed} failed`,
              });
            } else {
              toast({
                variant: 'success',
                title: `${batch.completed} resumes generated`,
              });
            }
            return;
          }

          pollRef.current = setTimeout(
            () => void pollOnce(),
            BATCH_POLL_INTERVAL
          );
        } catch {
          // Polling error — keep trying, but only after the previous
          // attempt fully unwound (no overlap).
          pollRef.current = setTimeout(
            () => void pollOnce(),
            BATCH_POLL_INTERVAL
          );
        }
      };

      pollRef.current = setTimeout(() => void pollOnce(), BATCH_POLL_INTERVAL);
    } catch {
      toast({ variant: 'error', title: 'Network error starting batch' });
      setGenerating(false);
      setBatchProgress(undefined);
    }
  }, [selectedIds, toast]);

  const handleBatchExport = useCallback(async () => {
    if (selectedIds.size === 0) return;
    setExporting(true);
    try {
      // Fetch resume IDs for selected jobs in parallel
      const results = await Promise.allSettled(
        [...selectedIds].map(jobId =>
          fetch(`/api/jobs/tailor/by-job/${jobId}`).then(async res => {
            if (!res.ok) return null;
            const record = (await res.json()) as {
              id: string;
              approved_at: string | null;
            };
            return record.approved_at ? record.id : null;
          })
        )
      );
      const resumeIds = results
        .filter(
          (r): r is PromiseFulfilledResult<string> =>
            r.status === 'fulfilled' && r.value !== null
        )
        .map(r => r.value);

      if (resumeIds.length === 0) {
        toast({ variant: 'warning', title: 'No approved resumes to export' });
        return;
      }

      const res = await fetch('/api/jobs/tailor/export-zip', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ resume_ids: resumeIds }),
      });

      if (!res.ok) {
        // ``/tailor/export-zip`` surfaces actionable detail (e.g.,
        // ``"resumes not yet approved: id1, id2"``) — without
        // ``extractApiError`` the user just saw "Export failed" and
        // had to guess which selected resume was the holdup.
        toast({
          variant: 'error',
          title: await extractApiError(res, 'Export failed'),
        });
        return;
      }

      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'resumes.zip';
      a.click();
      URL.revokeObjectURL(url);
      toast({
        variant: 'success',
        title: `Exported ${resumeIds.length} resumes`,
      });
    } catch {
      toast({ variant: 'error', title: 'Network error exporting resumes' });
    } finally {
      setExporting(false);
    }
  }, [selectedIds, toast]);

  const handleBatchDelete = useCallback(async () => {
    if (selectedIds.size === 0) return;
    // Remove from the target the user is actually looking at. On All Jobs
    // (``activeTargetId`` undefined) the API removes from every target that
    // holds the posting — there is no single target in scope there.
    await removeJobs([...selectedIds], activeTargetId, () =>
      setRefreshKey(k => k + 1)
    );
    setSelectedIds(new Set());
    setRefreshKey(k => k + 1);
    setConfirmBatchDeleteOpen(false);
  }, [selectedIds, removeJobs, activeTargetId]);

  /**
   * #10: apply one status to the whole selection. No confirm — unlike Remove,
   * a status change is a normal pipeline move and every status is reachable
   * again from the same menu, so a modal would be friction without recourse
   * value. The selection is kept so a mis-click is one more click to correct.
   */
  const handleBatchStatus = useCallback(
    async (status: JobStatus) => {
      if (selectedIds.size === 0) return;
      const changed = await setStatusForJobs([...selectedIds], status);
      if (changed > 0) setRefreshKey(k => k + 1);
    },
    [selectedIds, setStatusForJobs]
  );

  // When the action bar is visible it overlaps the bottom of the table /
  // pagination. Mobile bar is two-row (~5.5rem) + gap, desktop is single-row
  // (~3.25rem). Reserve enough space at each breakpoint to scroll past it.
  const bottomPadClass = selectedIds.size > 0 ? 'pb-28 md:pb-20' : '';

  return (
    <div className={`flex flex-col gap-6 ${bottomPadClass}`}>
      <div>
        <Heading variant='hero' as='h1'>
          Jobs
        </Heading>
        <Text variant='body' className='mt-1 text-text-secondary'>
          Postings matched to your active targets
        </Text>
      </div>

      {targets.length === 0 ? (
        <Card>
          <CardContent className='flex flex-col items-center gap-3 py-12'>
            <Text variant='body' as='p'>
              No active targets. Activate a target to start seeing matched jobs.
            </Text>
            <LinkButton
              name='jobs-go-to-targets'
              variant='primary'
              size='sm'
              href='/targets'
            >
              Go to Targets
            </LinkButton>
          </CardContent>
        </Card>
      ) : (
        <>
          {/* Toggle-button group rather than role='tablist'. Tabs require
              Left/Right/Home/End keyboard nav + aria-controls / tabpanel
              linkage per WAI-ARIA APG; we don't need either since the
              "panel" is the rest of the page. aria-pressed gives SR
              users the same selected-state announcement as aria-selected
              would. (Phase 5 A11y P2.) */}
          <div className='border-b border-border'>
            <div
              role='group'
              aria-label='Filter jobs by target'
              className='flex gap-1 overflow-x-auto'
            >
              <button
                type='button'
                aria-pressed={activeTargetId === undefined}
                onClick={() => handleTabChange(undefined)}
                className={cn(
                  'shrink-0 border-b-2 px-4 py-2.5 text-sm transition-colors',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2',
                  activeTargetId === undefined
                    ? 'border-brand-500 text-brand-500'
                    : 'border-transparent text-text-secondary hover:text-text-primary hover:border-border-secondary'
                )}
              >
                All Jobs
              </button>
              {targets.map(target => (
                <button
                  type='button'
                  key={target.id}
                  aria-pressed={activeTargetId === target.id}
                  onClick={() => handleTabChange(target.id)}
                  className={cn(
                    'shrink-0 border-b-2 px-4 py-2.5 text-sm transition-colors',
                    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2',
                    activeTargetId === target.id
                      ? 'border-brand-500 text-brand-500'
                      : 'border-transparent text-text-secondary hover:text-text-primary hover:border-border-secondary'
                  )}
                >
                  {target.label}
                </button>
              ))}
            </div>
          </div>

          {activeTargetId && activationStatus === 'deriving' && (
            <div className='flex items-center gap-2 text-sm text-text-secondary'>
              <Spinner size='sm' aria-label='Analyzing target' />
              <span>Analyzing target profile...</span>
            </div>
          )}

          {activeTargetId && activationStatus === 'polling' && (
            <div className='flex items-center gap-2 text-sm text-text-secondary'>
              <Spinner size='sm' aria-label='Searching for jobs' />
              <span>Searching for matching jobs...</span>
            </div>
          )}

          {activeTargetId && activationStatus === 'error' && (
            <div className='text-sm text-error'>
              Failed to load jobs for this target. Try switching tabs to retry.
            </div>
          )}

          <JobsListView
            filters={filters}
            onFiltersChange={setFilters}
            selectedIds={selectedIds}
            onSelectionChange={setSelectedIds}
            refreshKey={refreshKey}
            targetId={activeTargetId}
            analysisTargetId={activeTargetId ?? targets[0]?.id}
            onPostingsLoaded={setVisiblePostings}
            controlledTableState={controlledTableState}
            onTableSortChange={onTableSortChange}
            onJobAdded={() => setRefreshKey(k => k + 1)}
          />

          {/* Thin-results CTA. Empty state (0 jobs) is owned by
              JobsEmptyState inside JobsListTable / JobsListMobile;
              this one fires in the 1–4 range to keep the user from
              staring at a sparse list with no path to add more.
              Only shows once activation is in the ``ready`` state —
              while deriving / polling, the count is still settling
              and a "you have few jobs" affordance would be premature. */}
          {activeTargetId &&
            activationStatus === 'ready' &&
            jobsCount !== null &&
            jobsCount > 0 &&
            jobsCount < 5 && (
              <JobsThinResultsCallout
                jobsCount={jobsCount}
                targetLabel={
                  targets.find(t => t.id === activeTargetId)?.label ??
                  'this target'
                }
                onJobAdded={() => setRefreshKey(k => k + 1)}
              />
            )}

          <BatchActionBar
            selectedCount={selectedIds.size}
            onClear={() => setSelectedIds(new Set())}
            onBatchGenerate={handleBatchGenerate}
            onBatchDelete={() => setConfirmBatchDeleteOpen(true)}
            onBatchExport={handleBatchExport}
            onBatchStatus={handleBatchStatus}
            generating={generating}
            exporting={exporting}
            statusUpdating={statusUpdating}
            hasApproved={visiblePostings.some(
              p => selectedIds.has(p.id) && p.status === 'resume_ready'
            )}
            batchProgress={batchProgress}
          />

          <ConfirmModal
            isOpen={confirmBatchDeleteOpen}
            onClose={() => setConfirmBatchDeleteOpen(false)}
            onConfirm={handleBatchDelete}
            title='Remove jobs?'
            // The old copy said "This can't be undone", which was false — the
            // action archived a per-user row that any status change reversed.
            // Say what actually happens, and name the scope: removal is per
            // target, so the posting survives under the user's other targets.
            message={`Remove ${selectedIds.size} ${
              selectedIds.size === 1 ? 'job' : 'jobs'
            } from ${activeTargetLabel ?? 'your targets'}? ${
              selectedIds.size === 1 ? 'It' : 'They'
            } will stop appearing here. You can undo this.`}
            confirmLabel='Remove'
            destructive
            loading={batchDeleting}
            loadingLabel='Removing…'
          />
        </>
      )}
    </div>
  );
}
