'use client';

import {
  type KeyboardEvent as ReactKeyboardEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react';
import Link from 'next/link';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { ChevronDown } from 'lucide-react';
import { Input } from '@danieljoffe/shared-ui/Input';
import { Spinner } from '@danieljoffe/shared-ui/Spinner';
import { Text } from '@danieljoffe/shared-ui/Text';
import Button from '@/components/kit/Button';
import { extractApiError } from '@/lib/extractApiError';
import { timeAgo } from '@/lib/timeAgo';
import { useToast } from '@/state/Toast/ToastProvider';
import { addJobToTarget, createOrLinkTarget } from '../targets/targetFlows';
import type { JobSearchResponse, JobSearchResult } from './types';

const PAGE_SIZE = 20;

/** Recency filter presets. The value is the `posted_within` day-count carried in
 *  the URL + sent to the API (`''` = any time). */
const RECENCY_OPTIONS: { value: string; label: string }[] = [
  { value: '', label: 'Any time' },
  { value: '7', label: 'Past week' },
  { value: '30', label: 'Past month' },
  { value: '90', label: 'Past 3 months' },
];

/** Minimal projection of a user's target for the "Add to target" picker. */
interface TargetOption {
  id: string;
  label: string;
}

/** Shared (fetch-once) state for the user's targets, threaded to every row's
 *  picker so 20 rows don't each refetch `/api/targets/mine`. */
interface TargetsSource {
  targets: TargetOption[] | null;
  loading: boolean;
  error: string | null;
  ensureLoaded: () => void;
}

/** Two-letter monogram from a company name. */
function initials(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return '?';
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[words.length - 1][0]).toUpperCase();
}

/** Deterministic hue per company so the same company is always the same colour —
 *  a stable visual anchor for skimming (we don't have real logos: no company
 *  domain is stored, and `absolute_url` is the ATS board, not the company site). */
function hueFor(name: string): number {
  let h = 0;
  for (let i = 0; i < name.length; i += 1)
    h = (h * 31 + name.charCodeAt(i)) % 360;
  return h;
}

function CompanyAvatar({ name }: { name: string }) {
  // Solid colour + white text → self-contained, reads in light and dark themes.
  return (
    <span
      aria-hidden
      className='flex size-9 shrink-0 items-center justify-center rounded-md text-xs font-semibold text-white'
      style={{ backgroundColor: `hsl(${hueFor(name)} 48% 42%)` }}
    >
      {initials(name)}
    </span>
  );
}

/**
 * Per-row "Add to target" picker (#467 power-action). Opens a small menu of the
 * caller's targets (loaded once, lazily, on first open — see TargetsSource) and
 * adds THIS existing posting to the chosen one: the API scores the already-
 * ingested posting against that target and saves it to the pipeline. Distinct
 * from "Create target", which derives a whole NEW target from the listing URL.
 * Mirrors JobsLocationFilter's open / outside-click / Escape idiom for
 * consistent behaviour + a11y.
 */
function AddToTargetMenu({
  jobId,
  source,
}: {
  jobId: string;
  source: TargetsSource;
}) {
  const { toast } = useToast();
  const [open, setOpen] = useState(false);
  const [pendingId, setPendingId] = useState<string | null>(null);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const { targets, loading, error, ensureLoaded } = source;

  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next) ensureLoaded(); // lazy fetch on first open — side effect stays
    // out of the setState updater (which must be pure; ensureLoaded sets the
    // parent's state).
  };

  // Close on outside click / Escape — same idiom as JobsLocationFilter. The kit
  // Button isn't a forwardRef, so restore focus to the trigger by querying it
  // inside the wrapper rather than holding a ref to it.
  useEffect(() => {
    if (!open) return;
    function onDown(e: MouseEvent) {
      if (!wrapperRef.current?.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        setOpen(false);
        (
          wrapperRef.current?.querySelector(
            'button[aria-haspopup="menu"]'
          ) as HTMLElement | null
        )?.focus();
      }
    }
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const pick = async (target: TargetOption) => {
    setPendingId(target.id);
    try {
      await addJobToTarget(jobId, target.id);
      toast({
        variant: 'success',
        title: `Added to “${target.label}”`,
        description: 'Saved to your Jobs and scored against this target.',
      });
      setOpen(false);
    } catch (e) {
      toast({
        variant: 'error',
        title: 'Couldn’t add to target',
        ...(e instanceof Error ? { description: e.message } : {}),
      });
    } finally {
      setPendingId(null);
    }
  };

  return (
    <div ref={wrapperRef} className='relative'>
      <Button
        name='search-add-to-target'
        variant='secondary'
        size='sm'
        aria-haspopup='menu'
        aria-expanded={open}
        onClick={toggle}
      >
        Add to target
        <ChevronDown className='ml-1 size-3.5 text-text-tertiary' aria-hidden />
      </Button>
      {open && (
        <div
          role='menu'
          aria-label='Add to one of your targets'
          className='absolute left-0 z-20 mt-1 max-h-64 w-64 max-w-[calc(100vw-2rem)] overflow-auto rounded-lg border border-border bg-surface-elevated p-1 shadow-lg'
        >
          {loading && (
            <div className='flex items-center gap-2 px-2 py-1.5' role='status'>
              <Spinner size='sm' aria-label='Loading targets' />
              <Text variant='meta'>Loading targets…</Text>
            </div>
          )}
          {error && !loading && (
            <Text variant='error' role='alert' className='block px-2 py-1.5'>
              {error}
            </Text>
          )}
          {targets && !loading && targets.length === 0 && (
            <Text
              variant='meta'
              className='block px-2 py-1.5 text-text-secondary'
            >
              No targets yet — create one first.
            </Text>
          )}
          {targets &&
            !loading &&
            targets.map(t => (
              <button
                key={t.id}
                type='button'
                role='menuitem'
                onClick={() => pick(t)}
                disabled={pendingId !== null}
                className='block w-full truncate rounded-md px-2 py-1.5 text-left text-sm text-text-primary hover:bg-surface-tertiary/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 disabled:opacity-60'
              >
                {pendingId === t.id ? 'Adding…' : t.label}
              </button>
            ))}
        </div>
      )}
    </div>
  );
}

/**
 * Manual keyword job search (#467). Kept deliberately DISTINCT from the matched
 * Jobs view: no match scores here (results aren't ranked against the user's
 * profile), so search is never mistaken for the quality of the matching engine —
 * and "see your matches" stays the reason to use Jobs. Logged-in only.
 */
function JobSearchRow({
  job,
  targetsSource,
}: {
  job: JobSearchResult;
  targetsSource: TargetsSource;
}) {
  const { toast } = useToast();
  const [creating, setCreating] = useState(false);
  const meta = [job.company_name, job.location].filter(Boolean).join(' · ');

  // Create a target from this listing (#467 power-action). Reuses the existing
  // from-url flow: derives a scoring profile in the background, DEDUPS against
  // your existing targets, links inactive (so it never trips the active-target
  // cap), and registers the company's ATS board to grow the corpus. Needs an
  // experience profile — the from-url endpoint 422s otherwise, surfaced here.
  const createTarget = async () => {
    if (!job.absolute_url) return;
    setCreating(true);
    try {
      const result = await createOrLinkTarget(
        '/api/targets/from-url',
        { jd_url: job.absolute_url },
        'Couldn’t create target'
      );
      toast({
        variant: 'success',
        title: result.was_matched
          ? `Linked to “${result.target.label}”`
          : `Target created: “${result.target.label}”`,
        description:
          'Building your match profile in the background — activate it under Targets to see your matches.',
      });
    } catch (e) {
      toast({
        variant: 'error',
        title: 'Couldn’t create target',
        ...(e instanceof Error ? { description: e.message } : {}),
      });
    } finally {
      setCreating(false);
    }
  };

  return (
    <li className='p-3 transition-colors hover:bg-surface-tertiary/60 motion-reduce:transition-none'>
      <div className='flex items-start gap-3'>
        <CompanyAvatar name={job.company_name} />
        <div className='min-w-0 flex-1'>
          {/* Line 1: role + salary */}
          <div className='flex items-start justify-between gap-3'>
            {job.absolute_url ? (
              <a
                href={job.absolute_url}
                target='_blank'
                rel='noopener noreferrer'
                className='font-medium underline underline-offset-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2'
              >
                {job.title}
              </a>
            ) : (
              <span className='font-medium'>{job.title}</span>
            )}
            {job.salary_text && (
              <Text variant='meta' className='shrink-0 text-text-secondary'>
                {job.salary_text}
              </Text>
            )}
          </div>
          {/* Line 2: company · location + posted date */}
          <div className='mt-0.5 flex items-baseline justify-between gap-3'>
            {meta && (
              <Text variant='meta' className='truncate text-text-secondary'>
                {meta}
              </Text>
            )}
            <Text variant='meta' className='shrink-0 text-text-tertiary'>
              {timeAgo(job.created_at)}
            </Text>
          </div>
          {/* Actions live BELOW the listing info (not beside it) so the info
              reads as one unit when skimming — avoids the awkward right-edge
              break against the salary/date. "Create target" derives a new
              target from this listing's URL; "Add to target" scores it against
              one you already have. */}
          <div className='mt-2 flex flex-wrap items-center gap-2'>
            {job.absolute_url && (
              <Button
                name='search-create-target'
                variant='secondary'
                size='sm'
                onClick={createTarget}
                disabled={creating}
                aria-busy={creating}
              >
                {creating ? 'Creating…' : 'Create target'}
              </Button>
            )}
            <AddToTargetMenu jobId={job.id} source={targetsSource} />
          </div>
        </div>
      </div>
    </li>
  );
}

export default function JobSearchExplorer() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  // The URL is the SINGLE SOURCE OF TRUTH for the search state — query + filters
  // live in the querystring so back/forward and bookmarks restore an exact
  // search (a user can leave for a listing and come back to the same results).
  const urlQ = (searchParams.get('q') ?? '').trim();
  const urlLocation = searchParams.get('location') ?? '';
  const urlDays = searchParams.get('posted_within') ?? '';

  // Text-input drafts, seeded from the URL and re-synced whenever it changes
  // underneath us (back/forward). Submitting commits them back to the URL.
  const [draftQ, setDraftQ] = useState(urlQ);
  const [draftLocation, setDraftLocation] = useState(urlLocation);

  const [results, setResults] = useState<JobSearchResult[] | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false); // fresh search
  const [loadingMore, setLoadingMore] = useState(false); // "Load more"
  const [error, setError] = useState<string | null>(null);

  // The user's targets for every row's "Add to target" picker — fetched ONCE,
  // lazily, the first time any menu opens. ``targetsRequested`` stops the N
  // rows from each kicking a fetch; it resets on failure so a later open retries.
  const [targets, setTargets] = useState<TargetOption[] | null>(null);
  const [targetsLoading, setTargetsLoading] = useState(false);
  const [targetsError, setTargetsError] = useState<string | null>(null);
  const targetsRequested = useRef(false);

  const ensureTargets = useCallback(() => {
    if (targetsRequested.current) return;
    targetsRequested.current = true;
    setTargetsLoading(true);
    setTargetsError(null);
    void (async () => {
      try {
        const res = await fetch('/api/targets/mine');
        if (!res.ok) {
          throw new Error(await extractApiError(res, 'Failed to load targets'));
        }
        const data = (await res.json()) as {
          targets: { target: { id: string; label: string } }[];
        };
        setTargets(
          data.targets.map(t => ({ id: t.target.id, label: t.target.label }))
        );
      } catch (e) {
        targetsRequested.current = false; // allow a retry on the next open
        setTargetsError(
          e instanceof Error ? e.message : 'Failed to load targets.'
        );
      } finally {
        setTargetsLoading(false);
      }
    })();
  }, []);

  const targetsSource: TargetsSource = {
    targets,
    loading: targetsLoading,
    error: targetsError,
    ensureLoaded: ensureTargets,
  };

  const fetchPage = useCallback(
    async (opts: {
      q: string;
      location: string;
      days: string;
      offset: number;
    }) => {
      const sp = new URLSearchParams({
        q: opts.q,
        page_size: String(PAGE_SIZE),
        offset: String(opts.offset),
      });
      if (opts.location.trim()) sp.set('location', opts.location.trim());
      if (opts.days) sp.set('posted_within_days', opts.days);
      const res = await fetch(`/api/jobs/search?${sp.toString()}`);
      if (!res.ok) throw new Error(await extractApiError(res, 'Search failed'));
      return (await res.json()) as JobSearchResponse;
    },
    []
  );

  // Run the search whenever the URL search-state changes: initial mount, a
  // submit/filter change (which commits to the URL), or back/forward. This is
  // the ONLY place a fresh (page-0) search is kicked, so the URL and the results
  // never drift. An empty query renders the honest empty page.
  useEffect(() => {
    setDraftQ(urlQ);
    setDraftLocation(urlLocation);
    if (!urlQ) {
      setResults(null);
      setError(null);
      setHasMore(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    setResults(null);
    fetchPage({ q: urlQ, location: urlLocation, days: urlDays, offset: 0 })
      .then(data => {
        if (cancelled) return;
        setResults(data.results);
        setHasMore(data.has_more);
      })
      .catch(e => {
        if (cancelled) return;
        setError(
          e instanceof Error ? e.message : 'Network error running search.'
        );
        setResults(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [urlQ, urlLocation, urlDays, fetchPage]);

  // Commit search state to the URL (replace → the /search entry updates in place,
  // so "back" from a listing lands on this exact query+filters rather than a
  // stack of every keystroke). The effect above re-runs the search off the new
  // URL. `scroll:false` keeps the viewport put on a filter tweak.
  const commit = useCallback(
    (next: { q: string; location: string; days: string }) => {
      const params = new URLSearchParams();
      if (next.q.trim()) params.set('q', next.q.trim());
      if (next.location.trim()) params.set('location', next.location.trim());
      if (next.days) params.set('posted_within', next.days);
      const qs = params.toString();
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    },
    [router, pathname]
  );

  const submitSearch = () =>
    commit({ q: draftQ, location: draftLocation, days: urlDays });
  const changeRecency = (days: string) =>
    commit({ q: draftQ, location: draftLocation, days });
  const clearFilters = () => commit({ q: draftQ, location: '', days: '' });

  const loadMore = useCallback(async () => {
    if (!results) return;
    setLoadingMore(true);
    setError(null);
    try {
      const data = await fetchPage({
        q: urlQ,
        location: urlLocation,
        days: urlDays,
        offset: results.length,
      });
      setResults(prev => [...(prev ?? []), ...data.results]);
      setHasMore(data.has_more);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Network error loading more.');
    } finally {
      setLoadingMore(false);
    }
  }, [fetchPage, urlQ, urlLocation, urlDays, results]);

  const hasActiveFilters = Boolean(urlLocation.trim() || urlDays);
  const onEnter = (e: ReactKeyboardEvent) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      submitSearch();
    }
  };

  return (
    <div className='mx-auto max-w-3xl space-y-6'>
      <header className='space-y-1'>
        <h1 className='text-2xl font-semibold text-text-primary'>
          Search jobs
        </h1>
        <Text variant='body' className='text-text-secondary'>
          Browse the full job pool by keyword. These results aren’t scored
          against your profile — head to{' '}
          <Link href='/jobs' className='underline underline-offset-2'>
            Jobs
          </Link>{' '}
          to see how you match.
        </Text>
      </header>

      <div className='space-y-2'>
        <div className='flex gap-2'>
          <div className='flex-1'>
            <Input
              value={draftQ}
              onChange={e => setDraftQ(e.target.value)}
              onKeyDown={onEnter}
              placeholder='e.g. senior frontend engineer'
              aria-label='Search jobs by title or keyword'
            />
          </div>
          <Button
            name='job-search-submit'
            onClick={submitSearch}
            disabled={loading || !draftQ.trim()}
          >
            Search
          </Button>
        </div>

        {/* Filters — a compact row of location (substring) + recency, both
            small so they read as refinements under the main search. Both live
            in the URL so a filtered search is restorable. */}
        <div className='flex flex-wrap items-center gap-2'>
          {/* Wrap in a sized box — the shared-ui Input fills its parent (the
              search row wraps it in flex-1 for the same reason), so a bare
              width class on the Input alone leaves a full-width wrapper that
              pushes the date select onto its own line. */}
          <div className='w-40'>
            <Input
              value={draftLocation}
              onChange={e => setDraftLocation(e.target.value)}
              onKeyDown={onEnter}
              placeholder='Location'
              aria-label='Filter by location'
            />
          </div>
          <label className='sr-only' htmlFor='job-search-recency'>
            Filter by date posted
          </label>
          <select
            id='job-search-recency'
            value={urlDays}
            // h-12 + text-base match the shared-ui Input's height/font so the
            // two filter controls line up (the Input renders taller than a
            // default-padded select).
            onChange={e => changeRecency(e.target.value)}
            className='h-12 rounded-md border border-border bg-surface-base px-3 text-base text-text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500'
          >
            {RECENCY_OPTIONS.map(o => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
          {hasActiveFilters && (
            <button
              type='button'
              name='job-search-clear-filters'
              onClick={clearFilters}
              className='rounded text-sm text-text-secondary underline underline-offset-2 hover:text-text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500'
            >
              Clear filters
            </button>
          )}
        </div>
      </div>

      {loading && (
        <div
          className='flex items-center gap-2'
          role='status'
          aria-live='polite'
        >
          <Spinner size='sm' aria-label='Searching' />
          <Text variant='meta'>Searching…</Text>
        </div>
      )}

      {error && !loading && (
        <Text variant='error' role='alert'>
          {error}
        </Text>
      )}

      {!loading && !error && results !== null && (
        <section aria-live='polite'>
          {results.length === 0 ? (
            <div className='space-y-1'>
              <Text variant='body'>
                No roles match “{urlQ}”
                {hasActiveFilters ? ' with these filters' : ''} yet.
              </Text>
              <Text variant='meta' className='text-text-secondary'>
                {hasActiveFilters
                  ? 'Try widening the location or date range, or a broader title.'
                  : 'We’re expanding coverage — try a broader title, or check back soon.'}
              </Text>
            </div>
          ) : (
            <>
              <ul className='divide-y divide-border overflow-hidden rounded-md border border-border'>
                {results.map(job => (
                  <JobSearchRow
                    key={job.id}
                    job={job}
                    targetsSource={targetsSource}
                  />
                ))}
              </ul>
              {hasMore && (
                <div className='mt-4 flex justify-center'>
                  <Button
                    name='job-search-load-more'
                    variant='secondary'
                    onClick={loadMore}
                    disabled={loadingMore}
                    aria-busy={loadingMore}
                  >
                    {loadingMore ? 'Loading…' : 'Load more'}
                  </Button>
                </div>
              )}
            </>
          )}
        </section>
      )}
    </div>
  );
}
