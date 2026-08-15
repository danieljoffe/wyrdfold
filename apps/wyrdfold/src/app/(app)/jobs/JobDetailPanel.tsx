'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { ChevronDown, Maximize2, MoreVertical } from 'lucide-react';
import { Badge } from '@danieljoffe/shared-ui/Badge';
import { Dropdown } from '@danieljoffe/shared-ui/Dropdown';
import type { DropdownItem } from '@danieljoffe/shared-ui/Dropdown';
import { Skeleton } from '@danieljoffe/shared-ui/Skeleton';
import { Spinner } from '@danieljoffe/shared-ui/Spinner';
import { Text } from '@danieljoffe/shared-ui/Text';
import Button from '@/components/kit/Button';
import LinkButton from '@/components/kit/LinkButton';
import ConfirmModal from '@/components/ConfirmModal';
import ScoreBadge from '@/components/ScoreBadge';
import { LocalDate } from '@/components/LocalFormat';
import { formatCompanyName } from '@/lib/formatCompanyName';
import { cn } from '@/lib/cn';
import { displayTitle } from '@/lib/displayTitle';
import { extractApiError } from '@/lib/extractApiError';
import { useToast } from '@/state/Toast/ToastProvider';
import CoverLetterSection from './CoverLetterSection';
import { useJobDelete } from './useJobDelete';
import JobFeedbackSection from './JobFeedbackSection';
import LogisticsChips from './LogisticsChips';
import ResumeSection from './ResumeSection';
import StatusIndicator from './StatusIndicator';
import {
  formatStatus,
  JOB_STATUSES,
  STATUS_DOT_CLASS,
  postedAt,
  type AnalysisStatus,
  type JobAnalysis,
  type JobPosting,
  type JobStatus,
  type StatusLogEntry,
} from './types';

/** Poll cadence + ceiling for the backgrounded analysis (#459). The LLM run
 *  is ~26s, so ~2.5s polls resolve in ~10 round-trips; the ceiling (~2min)
 *  bounds a stuck run into a retryable error rather than an infinite loop. */
const ANALYSIS_POLL_INTERVAL_MS = 2500;
const ANALYSIS_MAX_POLLS = 48;

const delay = (ms: number) =>
  new Promise<void>(resolve => setTimeout(resolve, ms));

interface JobDetailPanelProps {
  posting: JobPosting;
  targetId: string | undefined;
  viewFullHref: string | undefined;
  onDelete: (() => void) | undefined;
  onStatusChange: ((status: string) => void) | undefined;
  /** Fired after the LLM analysis completes. The blend write-back
   *  (PR #689 / #690 / #691) updates the per-target score + flips
   *  ``scoring_status`` to ``complete``, but the panel's ``posting``
   *  prop is still stale until the parent refetches. Pages that own
   *  ``posting`` state should re-GET ``/api/jobs/{id}`` here so the
   *  Score badge + breakdown reflect the new blended values without
   *  the user having to manually refresh. */
  onAnalysisComplete?: (() => void) | undefined;
  /** Suppress the panel's own Delete action (the page renders one at root). */
  hideDelete?: boolean;
  /** Default-open the JD description block on the full-page detail
   *  view (where the user clearly wants to see it). The inline panel
   *  in the list keeps it collapsed to avoid blowing up rows. */
  defaultDescriptionOpen?: boolean;
}

const SCORE_FACTOR_LABEL: Record<string, string> = {
  role_titles: 'Role titles',
  technologies: 'Technologies',
  domain_skills: 'Domain skills',
  seniority_signals: 'Seniority signals',
  negative: 'Penalties',
};

function formatFactor(key: string): string {
  return SCORE_FACTOR_LABEL[key] ?? key.replace(/_/g, ' ');
}

/** Fixed render order — the four axes whose average IS the match score (#609). */
const FIT_AXES: ReadonlyArray<[key: string, label: string]> = [
  ['title_fit', 'Title match'],
  ['skills_fit', 'Skills match'],
  ['seniority_fit', 'Seniority match'],
  ['domain_fit', 'Domain match'],
];

/**
 * Breakdown for GRADED rows: the fit grade's axes on the same 0–100 scale
 * as the score, so the section finally explains the number beside it. The
 * keyword components (ScoreBreakdownList) stay for pending rows — there the
 * keyword score IS the whole story (#47).
 */
function FitAxisList({ axes }: { axes: Record<string, number> }) {
  const known = FIT_AXES.filter(([key]) => typeof axes[key] === 'number');
  if (known.length === 0) {
    return <Text variant='meta'>No match axes recorded for this grade</Text>;
  }
  return (
    <ul className='flex flex-col gap-2'>
      {known.map(([key, label]) => {
        const value = axes[key] as number;
        return (
          <li key={key} className='flex flex-col gap-1'>
            <div className='flex items-baseline justify-between gap-3'>
              <span className='text-sm text-text-primary'>{label}</span>
              <span className='text-xs font-medium tabular-nums shrink-0 text-text-secondary'>
                {Math.round(value)}
              </span>
            </div>
            <div className='h-1.5 w-full overflow-hidden rounded-full bg-surface-elevated'>
              <div
                className='h-full rounded-full bg-success'
                style={{ width: `${Math.max(0, Math.min(100, value))}%` }}
              />
            </div>
          </li>
        );
      })}
    </ul>
  );
}

function ScoreBreakdownList({
  breakdown,
  rawScore,
  displayedScore,
  postedAtIso,
}: {
  breakdown: Record<string, number>;
  /** Undecayed fit. Absent on responses predating #665's projection. */
  rawScore: number | null | undefined;
  /** What the card actually shows — fit × freshness. */
  displayedScore: number;
  postedAtIso: string;
}) {
  // #650: the old list rendered RAW keyword points and hid zeros, so "+80"
  // and "+4" sat under a headline of 60 with no stated relationship. Two
  // different unit changes separate those numbers:
  //   components (raw points) → fit% (÷ this target's max) → × freshness
  // Show the whole chain so the panel reconciles to the number on the card.
  const entries = Object.entries(breakdown);
  if (entries.length === 0) {
    return <Text variant='meta'>No factors contributed to this score</Text>;
  }

  const rawTotal = entries.reduce((sum, [, v]) => sum + v, 0);
  // Apportion the fit score across components by their share of the raw
  // total. Derived from the two numbers we already have rather than
  // re-deriving the normalizer client-side — the server owns that formula.
  const canApportion =
    typeof rawScore === 'number' && rawScore > 0 && rawTotal > 0;
  const shareOf = (value: number) =>
    canApportion ? (value / rawTotal) * rawScore : value;

  // The freshness multiplier is OBSERVABLE (displayed ÷ fit), so the decay
  // formula is never duplicated here — if the server changes it, this follows.
  const freshness =
    typeof rawScore === 'number' && rawScore > 0
      ? displayedScore / rawScore
      : 1;
  const ageDays = Math.max(
    0,
    Math.round((Date.now() - new Date(postedAtIso).getTime()) / 86_400_000)
  );

  // Bars are a share of the FIT total, not of the largest entry — the old
  // max-of-present scale made the biggest component full-width by definition,
  // implying a denominator that did not exist.
  const barBase = canApportion
    ? rawScore
    : Math.max(...entries.map(([, v]) => Math.abs(v)));

  return (
    <div className='flex flex-col gap-2'>
      <ul className='flex flex-col gap-2'>
        {entries.map(([key, value]) => {
          const shown = shareOf(value);
          const pct =
            barBase === 0
              ? 0
              : Math.min(100, (Math.abs(shown) / barBase) * 100);
          const positive = shown > 0;
          const display = Number.isInteger(shown)
            ? shown
            : Number(shown.toFixed(1));
          return (
            <li key={key} className='flex flex-col gap-1'>
              <div className='flex items-baseline justify-between gap-3'>
                <span
                  className={cn(
                    'text-sm',
                    // A zero component is the most actionable signal on the
                    // card ("domain skills scored nothing") — it is shown, but
                    // muted so it does not compete with what did contribute.
                    shown === 0 ? 'text-text-tertiary' : 'text-text-primary'
                  )}
                >
                  {formatFactor(key)}
                </span>
                <span
                  className={cn(
                    'shrink-0 text-xs font-medium tabular-nums',
                    shown === 0
                      ? 'text-text-tertiary'
                      : positive
                        ? 'text-text-secondary'
                        : 'text-error'
                  )}
                >
                  {display}
                </span>
              </div>
              <div className='h-1.5 w-full rounded bg-surface-tertiary'>
                <div
                  className={cn(
                    'h-1.5 rounded',
                    positive ? 'bg-brand-500' : 'bg-error'
                  )}
                  style={{ width: `${pct}%` }}
                />
              </div>
            </li>
          );
        })}
      </ul>

      {canApportion && (
        <dl className='flex flex-col gap-1 border-t border-border pt-2 text-xs'>
          <div className='flex items-baseline justify-between gap-3'>
            <dt className='text-text-secondary'>Match against this target</dt>
            <dd className='tabular-nums font-medium text-text-primary'>
              {rawScore}
            </dd>
          </div>
          <div className='flex items-baseline justify-between gap-3'>
            <dt className='text-text-tertiary'>
              Freshness{ageDays > 0 ? ` (posted ${ageDays}d ago)` : ''}
            </dt>
            <dd className='tabular-nums text-text-tertiary'>
              ×{freshness.toFixed(2)}
            </dd>
          </div>
          <div className='flex items-baseline justify-between gap-3 border-t border-border pt-1'>
            <dt className='text-text-secondary'>Score shown</dt>
            <dd className='tabular-nums font-medium text-text-primary'>
              {displayedScore}
            </dd>
          </div>
        </dl>
      )}
    </div>
  );
}

export default function JobDetailPanel({
  posting,
  targetId,
  viewFullHref,
  onDelete,
  onStatusChange,
  onAnalysisComplete,
  hideDelete = false,
  defaultDescriptionOpen = false,
}: JobDetailPanelProps) {
  const [status, setStatus] = useState(posting.status);
  const [updating, setUpdating] = useState(false);
  const { deleteJob, deleting } = useJobDelete();
  const [confirmDeleteOpen, setConfirmDeleteOpen] = useState(false);
  const [analysis, setAnalysis] = useState<JobAnalysis | null>(null);
  // Whether the spend-free open probe (#634) has resolved. Gates the
  // "Analyze fit" button so it doesn't flash on jobs whose cached
  // scorecard is about to render.
  const [cacheChecked, setCacheChecked] = useState(false);
  const [analyzing, setAnalyzing] = useState(false);
  const [analyzingStartedAt, setAnalyzingStartedAt] = useState<number | null>(
    null
  );
  const [analyzingElapsedS, setAnalyzingElapsedS] = useState(0);
  const [analysisError, setAnalysisError] = useState<string | null>(null);
  // ``no_profile`` (404) isn't an error — the user just hasn't built their
  // experience profile yet. Tracked separately so we render a setup CTA
  // instead of a red error + pointless retry, and so the auto-trigger below
  // doesn't keep re-firing the (always-404) request. (#105)
  const [needsProfile, setNeedsProfile] = useState(false);
  const [history, setHistory] = useState<StatusLogEntry[]>([]);
  const { toast } = useToast();

  // Identity token for the in-flight analysis run (#459). Each kick-off sets a
  // fresh token; the async poll loop bails the moment ``activeRunRef`` no
  // longer points at its token — i.e. the component unmounted (set null below)
  // or a newer run superseded it (e.g. the target changed). This is what lets
  // the user navigate away mid-analysis without a dangling loop calling
  // setState on a gone component; the backend keeps running + persists, so
  // reopening the job picks the finished result straight off the cache.
  const activeRunRef = useRef<object | null>(null);
  useEffect(
    () => () => {
      activeRunRef.current = null;
    },
    []
  );

  const fetchHistory = useCallback(async () => {
    try {
      const res = await fetch(`/api/jobs/${posting.id}/status-history`);
      if (res.ok) {
        const data = (await res.json()) as { entries: StatusLogEntry[] };
        setHistory(data.entries);
      }
    } catch {
      // Non-critical — don't toast on history fetch failure
    }
  }, [posting.id]);

  useEffect(() => {
    fetchHistory();
  }, [fetchHistory]);

  async function updateStatus(newStatus: string) {
    setUpdating(true);
    try {
      const res = await fetch(`/api/jobs/${posting.id}/status`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: newStatus }),
      });
      if (res.ok) {
        setStatus(newStatus);
        onStatusChange?.(newStatus);
        fetchHistory();
      } else {
        toast({
          variant: 'error',
          title: await extractApiError(res, 'Failed to update status'),
        });
      }
    } catch {
      toast({ variant: 'error', title: 'Network error updating status' });
    } finally {
      setUpdating(false);
    }
  }

  // Tick the elapsed-seconds counter while an analysis is in flight. LLM
  // calls take 20–30s; without a moving number next to the section caption
  // the user has no signal between "click" and "result" and the panel
  // appears hung. The skeleton placeholder below the caption uses the same
  // ``bg-surface-tertiary`` as the panel surface, so it was invisible —
  // surfacing the indicator next to the caption guarantees something
  // moves regardless of the body fill.
  useEffect(() => {
    if (!analyzing || analyzingStartedAt === null) {
      setAnalyzingElapsedS(0);
      return;
    }
    const tick = () =>
      setAnalyzingElapsedS(
        Math.floor((Date.now() - analyzingStartedAt) / 1000)
      );
    tick();
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, [analyzing, analyzingStartedAt]);

  const runAnalysis = useCallback(
    async (kick = true) => {
      if (!targetId) return;
      // Supersede any prior loop and mark this one active.
      const token = {};
      activeRunRef.current = token;
      const alive = () => activeRunRef.current === token;

      const url = `/api/jobs/analysis/${posting.id}?target_id=${encodeURIComponent(targetId)}`;

      // A finished analysis record → render it + tell the parent to refetch.
      // The backend blended the LLM score into the per-target ``scores`` row and
      // flipped ``scoring_status`` to ``complete``, so the stale ``posting`` prop
      // (keyword-only score) needs a re-GET to refresh the badge + breakdown.
      const applyRecord = (record: JobAnalysis) => {
        setAnalysis(record);
        onAnalysisComplete?.();
      };

      setAnalyzing(true);
      setAnalyzingStartedAt(Date.now());
      setAnalysisError(null);
      setNeedsProfile(false);
      try {
        // ``kick`` (the default) is explicit user intent: POST kicks off (or
        // fetches a cached) analysis. Non-blocking (#459): a cache miss returns
        // 202 ``{status:"running"}`` and the LLM runs server-side in a detached
        // task, so this POST returns in well under a second. ``kick=false`` is
        // the spend-free open probe (#634) attaching to a run that is ALREADY
        // in flight — a GET, so opening a panel can never buy an LLM call.
        const res = await fetch(
          url,
          kick ? { method: 'POST' } : { method: 'GET' }
        );
        if (!alive()) return;
        if (!res.ok) {
          // ``no description`` (422) is a data gap, not a transient failure;
          // everything else routes through extractApiError (which understands
          // the structured ``llm_budget_exceeded`` 429).
          const message = await extractApiError(res, 'Analysis failed');
          if (alive()) {
            setAnalysisError(
              res.status === 422 && /no description/i.test(message)
                ? 'Analysis skipped — this job posting has no description text.'
                : message
            );
          }
          return;
        }

        const kicked = (await res.json()) as
          JobAnalysis | AnalysisStatus | { code?: string };
        if (!alive()) return;

        // ``no_profile`` is a setup state, not a failure — render the CTA (below)
        // rather than a red error + doomed retry. (#105)
        if ((kicked as { code?: string }).code === 'no_profile') {
          setNeedsProfile(true);
          return;
        }
        // Cache hit: the record came straight back — no polling needed.
        if ('id' in kicked) {
          applyRecord(kicked as JobAnalysis);
          return;
        }
        // A run was already failed server-side (rare on a kick) — surface it.
        if ((kicked as AnalysisStatus).status === 'error') {
          setAnalysisError(
            (kicked as AnalysisStatus).message ??
              'Analysis failed. Please retry.'
          );
          return;
        }

        // Otherwise it's ``running``: poll GET until the analysis lands. The work
        // continues + persists on the server regardless of this loop, so if the
        // user navigates away (``alive()`` → false) nothing is lost — reopening
        // the job hits the cache.
        //
        // Attach mode (kick=false) starts with its re-kick already spent: if the
        // watched run dies server-side, the panel hands back to the explicit
        // retry button rather than paying for a run the user never asked for.
        let reKicked = !kick;
        for (let attempt = 0; attempt < ANALYSIS_MAX_POLLS; attempt += 1) {
          await delay(ANALYSIS_POLL_INTERVAL_MS);
          if (!alive()) return;

          const poll = await fetch(url, { method: 'GET' });
          if (!alive()) return;
          if (!poll.ok) {
            setAnalysisError(await extractApiError(poll, 'Analysis failed'));
            return;
          }
          const data = (await poll.json()) as
            JobAnalysis | AnalysisStatus | { code?: string };
          if (!alive()) return;

          if ((data as { code?: string }).code === 'no_profile') {
            setNeedsProfile(true);
            return;
          }
          if ('id' in data) {
            applyRecord(data as JobAnalysis);
            return;
          }
          const status = (data as AnalysisStatus).status;
          if (status === 'error') {
            setAnalysisError(
              (data as AnalysisStatus).message ??
                'Analysis failed. Please retry.'
            );
            return;
          }
          if (status === 'idle') {
            // The server dropped the run (e.g. a deploy restarted the API
            // mid-analysis). Re-kick once; if it happens again, hand back to a
            // manual retry rather than looping forever.
            if (reKicked) {
              setAnalysisError('Analysis was interrupted. Please retry.');
              return;
            }
            reKicked = true;
            await fetch(url, { method: 'POST' });
            continue;
          }
          // ``running`` → keep polling.
        }
        // Ran out of poll attempts without a result.
        if (alive()) {
          setAnalysisError(
            'Analysis is taking longer than expected. Please retry.'
          );
        }
      } catch {
        if (alive()) setAnalysisError('Network error running analysis.');
      } finally {
        if (alive()) {
          setAnalyzing(false);
          setAnalyzingStartedAt(null);
        }
      }
    },
    [posting.id, targetId, onAnalysisComplete]
  );

  // Keep the latest runAnalysis reachable from the probe effect below without
  // widening that effect's deps: runAnalysis's identity churns with the
  // parent's ``onAnalysisComplete`` callback, and a probe keyed on it would
  // reset + re-GET on every parent render instead of once per (job, target).
  const runAnalysisRef = useRef(runAnalysis);
  useEffect(() => {
    runAnalysisRef.current = runAnalysis;
  });

  // Spend-free open (#634): opening the panel only READS. A cached record
  // renders instantly; a run already in flight (kicked earlier, possibly from
  // another surface) is attached to; a cache miss leaves the explicit
  // "Analyze fit" button. The auto-kick this replaces bought a ~$0.04 LLM run
  // plus a ~30s wait on every first open of a (job, target, version) with no
  // user intent — browsing must cost nothing until the user asks.
  useEffect(() => {
    if (!targetId) return;
    // New (job, target) identity — clear the previous one's states.
    setAnalysis(null);
    setAnalysisError(null);
    setNeedsProfile(false);
    setCacheChecked(false);
    const token = {};
    activeRunRef.current = token;
    const alive = () => activeRunRef.current === token;
    const url = `/api/jobs/analysis/${posting.id}?target_id=${encodeURIComponent(targetId)}`;
    void (async () => {
      try {
        const res = await fetch(url, { method: 'GET' });
        // A failed read-only probe just leaves the button — opening the
        // panel must never surface an error the user didn't cause.
        if (!alive() || !res.ok) return;
        const data = (await res.json()) as
          JobAnalysis | AnalysisStatus | { code?: string };
        if (!alive()) return;
        if ((data as { code?: string }).code === 'no_profile') {
          setNeedsProfile(true);
          return;
        }
        if ('id' in data) {
          // Cached record: render WITHOUT onAnalysisComplete — the score
          // blend happened on the run that produced it, so refetching the
          // posting on every open would be a wasted round-trip.
          setAnalysis(data as JobAnalysis);
          return;
        }
        const status = (data as AnalysisStatus).status;
        if (status === 'running') {
          // Already paid for and in flight — attach; never POST from here.
          setCacheChecked(true);
          void runAnalysisRef.current(false);
          return;
        }
        if (status === 'error') {
          // A prior run failed server-side: surface it with the retry button.
          setAnalysisError(
            (data as AnalysisStatus).message ?? 'Analysis failed. Please retry.'
          );
        }
        // ``idle`` → nothing cached, nothing running: the Analyze button.
      } catch {
        // Network error on the read-only probe → the button.
      } finally {
        if (alive()) setCacheChecked(true);
      }
    })();
  }, [posting.id, targetId]);

  async function handleDelete() {
    if (await deleteJob(posting.id)) {
      setConfirmDeleteOpen(false);
      onDelete?.();
    }
  }

  const breakdown = posting.score_breakdown;

  // Fit axes for the breakdown section (#609). List rows served by the RPC
  // paths can't carry ``axis_scores`` (undefined) until R3 adds the column
  // to their RETURNS TABLE — for a graded row, lazily pull the detail GET
  // (same pattern as ResumeSection/CoverLetterSection fetching the JD). A
  // failed fetch falls back to the keyword components rather than a hole.
  const [fetchedAxes, setFetchedAxes] = useState<Record<string, number> | null>(
    null
  );
  const [axesFetchDone, setAxesFetchDone] = useState(false);
  const needsAxesFetch =
    !posting.pending && posting.axis_scores === undefined && !axesFetchDone;
  useEffect(() => {
    if (!needsAxesFetch) return;
    let cancelled = false;
    void fetch(`/api/jobs/${posting.id}`)
      .then(res => (res.ok ? res.json() : null))
      .then(
        (detail: { axis_scores?: Record<string, number> | null } | null) => {
          if (cancelled) return;
          setFetchedAxes(detail?.axis_scores ?? null);
          setAxesFetchDone(true);
        }
      )
      .catch(() => {
        if (!cancelled) setAxesFetchDone(true);
      });
    return () => {
      cancelled = true;
    };
  }, [needsAxesFetch, posting.id]);
  const axes = posting.axis_scores ?? fetchedAxes;
  const axesPending = needsAxesFetch && !axesFetchDone;

  // Sanitize the upstream JD HTML once per posting. The body is THIRD-PARTY
  // (Greenhouse et al.) and merely passes through our poller, so it must be
  // treated as attacker-controlled at render time. ``sanitizeJobDescription``
  // decodes one level of entities (Greenhouse persists ``&lt;h4&gt;`` rather
  // than ``<h4>``) and then runs DOMPurify with an explicit allow-list that
  // mirrors the server's bleach config — see that module for the full
  // threat-model rationale (audit #29 R2-3).
  //
  // ``description_html`` is only populated on the /jobs/{id} detail
  // response — the /jobs list omits it. Dynamic-import keeps the
  // ~35 KB isomorphic-dompurify dep off the list-page bundle.
  const [sanitizedDescription, setSanitizedDescription] = useState<
    string | null
  >(null);
  useEffect(() => {
    const raw = posting.description_html ?? '';
    if (!raw.trim()) {
      setSanitizedDescription(null);
      return;
    }
    let cancelled = false;
    void Promise.all([
      import('isomorphic-dompurify'),
      import('./sanitizeJobDescription'),
    ]).then(([purifyMod, sanitizeMod]) => {
      if (cancelled) return;
      setSanitizedDescription(
        sanitizeMod.sanitizeJobDescriptionHtml(raw, purifyMod.default)
      );
    });
    return () => {
      cancelled = true;
    };
  }, [posting.description_html]);

  return (
    <div className='border-t border-border bg-surface-tertiary p-4 space-y-6'>
      {/* Single header toolbar:
            [status] | [score] | [resume] | [cover letter] | [open ↗] | [⋯]
          Tailor actions live in the toolbar rather than a separate row so the
          panel reads top-down as one strip of decisions plus the analysis
          body, instead of six labeled stacks. */}
      <div className='flex flex-wrap items-center gap-2 md:flex-nowrap md:gap-3'>
        <Dropdown
          trigger={
            <span
              className={cn(
                'inline-flex items-center gap-2 rounded-md border border-border bg-surface-elevated px-3 py-1.5 text-sm transition-colors',
                updating
                  ? 'opacity-50 cursor-not-allowed'
                  : 'hover:bg-surface-tertiary'
              )}
              aria-disabled={updating || undefined}
            >
              <span
                className={cn(
                  'inline-block size-2 rounded-full',
                  STATUS_DOT_CLASS[status as JobStatus] ?? 'bg-text-tertiary'
                )}
                aria-hidden
              />
              <span className='capitalize'>{formatStatus(status)}</span>
              <ChevronDown className='size-4 text-text-tertiary' aria-hidden />
            </span>
          }
          items={JOB_STATUSES.map<DropdownItem>(s => ({
            label: formatStatus(s),
            icon: (
              <span
                className={cn(
                  'inline-block size-2 rounded-full',
                  STATUS_DOT_CLASS[s]
                )}
                aria-hidden
              />
            ),
            disabled: updating || status === s,
            onClick: () => updateStatus(s),
          }))}
        />
        <ScoreBadge score={posting.score} size='sm' />

        {/* Resume + Cover Letter as single-button pills in the toolbar. Only
            when a target is selected — tailoring requires one. The components
            keep all their generate/review/view state internally. */}
        {targetId && (
          <>
            <ResumeSection
              jobPostingId={posting.id}
              onDrafted={() => {
                // Mirror the server's mark_job_resume_draft so the pill
                // doesn't show "New" until a reload (§B7). Forward-only:
                // never demote a job the user already advanced.
                if (status === 'new' || status === 'saved') {
                  setStatus('resume_draft');
                  onStatusChange?.('resume_draft');
                  fetchHistory();
                }
              }}
            />
            <CoverLetterSection
              jobPostingId={posting.id}
              companyName={formatCompanyName(posting.company_name)}
              roleTitle={displayTitle(posting)}
            />
          </>
        )}

        {/* The remaining icons push to the right of the toolbar. ``ml-auto``
            on the first right-aligned item lets the flex row wrap naturally
            on narrow viewports without splitting Status/Score from the
            tailor buttons. */}
        {viewFullHref && (
          <LinkButton
            href={viewFullHref}
            variant='bare'
            size='sm'
            name='view-full-job'
            aria-label='Open full view'
            className='text-text-secondary hover:bg-surface-elevated hover:text-text-primary ml-auto'
          >
            <Maximize2 className='size-4' aria-hidden />
          </LinkButton>
        )}
        {!hideDelete && (
          <Dropdown
            trigger={
              <span
                className={cn(
                  'inline-flex size-8 items-center justify-center rounded-md text-text-secondary hover:bg-surface-elevated hover:text-text-primary',
                  !viewFullHref && 'ml-auto'
                )}
                aria-label='More actions'
              >
                <MoreVertical className='size-4' aria-hidden />
                {/* Names the BUTTON shared-ui wraps this trigger in. The
                    aria-label above sits on a role-less <span>, where it
                    computes no accessible name — and it is the inner node
                    regardless, so the focusable button had none at all. */}
                <span className='sr-only'>More actions</span>
              </span>
            }
            items={[
              {
                label: deleting ? 'Deleting…' : 'Delete',
                danger: true,
                disabled: deleting,
                onClick: () => setConfirmDeleteOpen(true),
              },
            ]}
          />
        )}
      </div>

      <LogisticsChips
        filters={posting.logistics_filters}
        variant='full'
        className='mt-1'
      />

      <ConfirmModal
        isOpen={confirmDeleteOpen}
        onClose={() => setConfirmDeleteOpen(false)}
        onConfirm={handleDelete}
        title='Delete posting?'
        message={`Delete "${displayTitle(posting)}" from ${posting.company_name}? This can't be undone.`}
        confirmLabel='Delete'
        destructive
        loading={deleting}
        loadingLabel='Deleting…'
      />

      {/* Two-column main body: Score Breakdown on the left, LLM Analysis on
          the right. The previous stacked-section layout forced the eye down
          the page through six labeled blocks; pairing the two scoring panels
          keeps both visible without scrolling and reads as "here's why we
          score it, here's what the model thinks". */}
      <div className='grid grid-cols-1 gap-6 md:grid-cols-2'>
        {/* Score breakdown — graded rows show the fit axes (their average IS
            the score); pending rows show the keyword components (their score
            IS the keyword sum). Mixing the two was the #609 credibility bug:
            keyword components can never explain an axis-blend number. */}
        <div>
          <Text variant='caption' className='mb-2'>
            Score breakdown
          </Text>
          {axes && Object.keys(axes).length > 0 ? (
            <FitAxisList axes={axes} />
          ) : axesPending ? (
            <Skeleton variant='text' lines={3} />
          ) : breakdown ? (
            <ScoreBreakdownList
              breakdown={breakdown}
              rawScore={posting.raw_score}
              displayedScore={posting.score}
              postedAtIso={postedAt(posting)}
            />
          ) : (
            <Skeleton variant='text' lines={3} />
          )}
        </div>

        {/* LLM Analysis — only when a target is selected. The "pick a
            target" hint lives at the list level so it shows once, not per
            row. Rendered inline here as the right column of the body grid;
            the standalone section it occupied before is gone. */}
        {targetId && (
          <div>
            <div className='mb-1 flex items-center gap-2'>
              <Text variant='caption'>Match analysis</Text>
              {analyzing && (
                <span
                  className='inline-flex items-center gap-1.5'
                  role='status'
                  aria-live='polite'
                >
                  <Spinner size='sm' aria-label='Running match analysis' />
                  <Text variant='meta'>Running… {analyzingElapsedS}s</Text>
                </span>
              )}
            </div>
            {analysis ? (
              <div className='space-y-2'>
                <Text variant='body'>{analysis.recommendation}</Text>
                <div className='flex flex-wrap gap-2'>
                  <Badge
                    variant={
                      analysis.scorecard.seniority_fit === 'strong'
                        ? 'success'
                        : analysis.scorecard.seniority_fit === 'moderate'
                          ? 'warning'
                          : 'error'
                    }
                    size='sm'
                  >
                    Seniority: {analysis.scorecard.seniority_fit}
                  </Badge>
                  <Badge
                    variant={
                      analysis.scorecard.domain_fit === 'strong'
                        ? 'success'
                        : analysis.scorecard.domain_fit === 'moderate'
                          ? 'warning'
                          : 'error'
                    }
                    size='sm'
                  >
                    Domain: {analysis.scorecard.domain_fit}
                  </Badge>
                </div>
                {analysis.scorecard.skills_missing.length > 0 && (
                  <div>
                    <Text variant='meta' className='mb-1'>
                      Missing skills
                    </Text>
                    <div className='flex flex-wrap gap-1'>
                      {analysis.scorecard.skills_missing.map(skill => (
                        <Badge key={skill} variant='error' size='sm'>
                          {skill}
                        </Badge>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            ) : analyzing ? (
              // Backgrounded (#459): the ~26s LLM run continues + persists on
              // the server whether or not the user waits, so reassure them they
              // can leave rather than staring at a spinner. The pulsing bars use
              // ``bg-surface-elevated`` (not the shared ``<Skeleton>``, whose
              // ``bg-surface-tertiary`` matches the panel surface and rendered
              // invisibly) so something visibly moves against the backdrop.
              <div className='space-y-2'>
                <Text variant='body' className='text-text-secondary'>
                  Analyzing this job against your profile. This runs in the
                  background — feel free to browse other jobs and come back;
                  we&apos;ll have it ready for you.
                </Text>
                <div className='h-4 rounded-xs bg-surface-elevated animate-pulse motion-reduce:animate-none' />
                <div className='h-4 w-3/4 rounded-xs bg-surface-elevated animate-pulse motion-reduce:animate-none' />
              </div>
            ) : needsProfile ? (
              <div className='space-y-2'>
                <Text variant='body' className='text-text-secondary'>
                  Set up your experience profile to see how you match this job.
                </Text>
                <Link
                  href='/profile'
                  className='inline-block text-sm font-medium underline underline-offset-2'
                >
                  Set up your profile →
                </Link>
              </div>
            ) : !cacheChecked ? (
              // The sub-second cache probe is still in flight — don't flash
              // the Analyze button at a job whose cached scorecard is about
              // to render.
              <div className='h-4 w-3/4 rounded-xs bg-surface-elevated animate-pulse motion-reduce:animate-none' />
            ) : (
              // Spend-free open (#634): nothing cached — analysis runs only
              // on this explicit click, never as a side effect of browsing.
              <div className='space-y-2'>
                {analysisError ? (
                  <Text variant='error'>{analysisError}</Text>
                ) : (
                  <Text variant='body' className='text-text-secondary'>
                    See how you match this job on skills, seniority and domain,
                    graded against your experience profile.
                  </Text>
                )}
                <Button
                  name='analyze-job'
                  variant='secondary'
                  size='sm'
                  onClick={() => void runAnalysis()}
                >
                  {analysisError ? 'Retry analysis' : 'Analyze match'}
                </Button>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Job description body — rendered from the upstream JD HTML.
          Wrapped in ``<details>`` so the inline list panel stays compact;
          the full detail page passes ``defaultDescriptionOpen`` to open
          it by default since the user navigated there explicitly. */}
      {sanitizedDescription && (
        <details open={defaultDescriptionOpen}>
          <summary className='cursor-pointer text-text-secondary hover:text-text-primary'>
            <Text variant='caption' as='span'>
              Job description
            </Text>
          </summary>
          <div
            className='mt-2 prose prose-sm dark:prose-invert max-w-none text-text-primary [&_a]:text-brand-500 [&_a]:underline [&_ul]:list-disc [&_ol]:list-decimal [&_ul]:pl-5 [&_ol]:pl-5'
            // Sanitized via DOMPurify above — safe to inject.
            dangerouslySetInnerHTML={{ __html: sanitizedDescription }}
          />
        </details>
      )}

      {/* Relevance feedback — only shown when viewing under a specific
          target, since the signal is target-scoped. */}
      {targetId && (
        <JobFeedbackSection jobId={posting.id} targetId={targetId} />
      )}

      {/* Status History */}
      {history.length > 0 && (
        <div>
          <Text variant='caption' className='mb-1'>
            History
          </Text>
          <div className='flex flex-col gap-1'>
            {history.slice(0, 5).map(entry => (
              <div
                key={entry.id}
                className='flex items-center gap-2 text-xs text-text-secondary'
              >
                <LocalDate
                  className='shrink-0'
                  value={entry.created_at}
                  options={{
                    month: 'short',
                    day: 'numeric',
                    hour: 'numeric',
                    minute: '2-digit',
                  }}
                />
                <span>&rarr;</span>
                <StatusIndicator status={entry.new_status} />
                {entry.note && (
                  <span className='truncate italic'>{entry.note}</span>
                )}
              </div>
            ))}
            {history.length > 5 && (
              <Text variant='meta' className='text-text-tertiary'>
                +{history.length - 5} more
              </Text>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
