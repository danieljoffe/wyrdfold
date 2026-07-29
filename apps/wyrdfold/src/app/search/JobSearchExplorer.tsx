'use client';

import {
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react';
import Link from 'next/link';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { Check, ChevronDown, Plus } from 'lucide-react';
import { Avatar } from '@danieljoffe/shared-ui/Avatar';
import { Badge } from '@danieljoffe/shared-ui/Badge';
import type { ButtonVariant } from '@danieljoffe/shared-ui/Button';
import { Heading } from '@danieljoffe/shared-ui/Heading';
import { Input } from '@danieljoffe/shared-ui/Input';
import { Select, type SelectOption } from '@danieljoffe/shared-ui/Select';
import { Spinner } from '@danieljoffe/shared-ui/Spinner';
import { Text } from '@danieljoffe/shared-ui/Text';
import Button from '@/components/kit/Button';
import { extractApiError } from '@/lib/extractApiError';
import { formatLocation } from '@/lib/formatLocation';
import { timeAgo } from '@/lib/timeAgo';
import { useToast } from '@/state/Toast/ToastProvider';
// `/search` now lives at the top level (auth-adaptive), so the targets flow —
// which stays in the authed `(app)` group — is imported by absolute alias
// rather than the old `../targets` relative path.
import { addJobToTarget } from '@/app/(app)/targets/targetFlows';
import { emitSearchEvent } from './searchEvents';
import type {
  JobSearchResponse,
  JobSearchResult,
  TargetMembershipResponse,
  TargetRef,
} from './types';
import type { TargetOption, TargetsSource } from './useTargetsSource';

const PAGE_SIZE = 20;

/** Recency filter presets. The value is the `posted_within` day-count carried in
 *  the URL + sent to the API (`''` = any time). */
const RECENCY_OPTIONS: SelectOption[] = [
  { value: '', label: 'Any time' },
  { value: '7', label: 'Past week' },
  { value: '30', label: 'Past month' },
  { value: '90', label: 'Past 3 months' },
];

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

export function CompanyAvatar({
  name,
  size = 'md',
}: {
  name: string;
  /** `md` on the card grid, `lg` in the detail modal header (#467 §11.2). */
  size?: 'md' | 'lg';
}) {
  // Shared-ui Avatar (square) carrying the deterministic per-company hue. Avatar
  // exposes no per-instance colour prop — only a static `tileClassName` — so the
  // computed hsl() rides a CSS variable set on the root and consumed by the inner
  // tile via `bg-[var(...)]` (a literal class Tailwind emits). Solid colour +
  // white text stays self-contained and reads in light and dark themes.
  return (
    <Avatar
      aria-hidden
      initials={initials(name)}
      shape='square'
      size={size}
      className='shrink-0'
      tileClassName='bg-[var(--company-hue)] font-semibold text-white'
      style={
        { '--company-hue': `hsl(${hueFor(name)} 48% 42%)` } as CSSProperties
      }
    />
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
export function AddToTargetMenu({
  jobId,
  source,
  variant = 'secondary',
  label = 'Add to target',
  onAdded,
}: {
  jobId: string;
  source: TargetsSource;
  /** Button emphasis — `primary` for the unbound modal's bind CTA (#467 §11.3). */
  variant?: ButtonVariant;
  /** Trigger label — `Add to another target` once the listing is already bound. */
  label?: string;
  /** Called after a successful add so the caller can reflect the new membership
   *  live — flipping the detail modal to its bound state (unlocking match /
   *  tailor) and badging the card without waiting for the next search. */
  onAdded?: (target: TargetOption) => void;
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
        // Close the menu only — swallow the Escape so a hosting Modal (which
        // listens for Escape on window) doesn't also close on the same
        // keystroke. Menu closed → its listener is gone → Escape closes the
        // modal, giving the expected nested-dismiss order (#467 §11).
        e.stopPropagation();
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
      onAdded?.(target);
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
        variant={variant}
        size='sm'
        aria-haspopup='menu'
        aria-expanded={open}
        onClick={toggle}
      >
        {label}
        <ChevronDown className='ml-1 size-3.5 opacity-70' aria-hidden />
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
 * A single search result (#467 §11.1). Kept deliberately DISTINCT from the
 * matched Jobs view: no match score here (results aren't ranked against the
 * user's profile), so search is never mistaken for the matching engine — "see
 * your matches" stays the reason to use Jobs.
 *
 * The WHOLE card is the click target → the listing detail (§11.1): no per-card
 * external link, no per-card conversion hook (browse-first). Since the detail
 * became URL-addressable (§11.2 fast-follow) the card is a REAL `<Link>` to
 * `/search/<id>`: a soft navigation the `@modal/(.)[id]` interception renders
 * as the modal over this grid, while copy-link / middle-click / a hard load
 * get the full page. Keyboard + role semantics come free with the anchor (the
 * footer badge is presentational — clicking anywhere on the card navigates).
 * `scroll={false}` keeps the grid's scroll position under the modal.
 */
function JobSearchCard({
  job,
  inTargets,
  isAuthenticated,
}: {
  job: JobSearchResult;
  inTargets: TargetRef[];
  /** Logged-out cards are a PURE click-target — no pipeline-state / add footer
   *  (#467 §11.1). The soft signup allusion lives only in the detail (§11.5). */
  isAuthenticated: boolean;
}) {
  const meta = [job.company_name, formatLocation(job)]
    .filter(Boolean)
    .join(' · ');
  const bound = inTargets.length > 0;

  return (
    <Link
      href={`/search/${job.id}`}
      scroll={false}
      aria-label={`Open ${job.title} at ${job.company_name}`}
      onClick={() => {
        // Funnel tick (§10 PR6) — fire-and-forget (keepalive), never blocks
        // or delays the navigation into the detail.
        emitSearchEvent({
          event_type: 'card_open',
          surface: isAuthenticated ? 'authed' : 'public',
          job_posting_id: job.id,
        });
      }}
      className='group flex cursor-pointer flex-col gap-3 rounded-lg border border-border bg-surface-elevated p-4 text-left shadow-sm transition hover:-translate-y-0.5 hover:shadow-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2 motion-reduce:transform-none motion-reduce:transition-none'
    >
      {/* header: company avatar, then role over company · location */}
      <div className='flex items-start gap-3'>
        <CompanyAvatar name={job.company_name} />
        <div className='min-w-0 flex-1'>
          <span className='font-semibold transition-colors group-hover:text-text-brand'>
            {job.title}
          </span>
          {meta && (
            <Text
              variant='meta'
              className='mt-0.5 block truncate text-text-secondary'
            >
              {meta}
            </Text>
          )}
        </div>
      </div>
      {/* snippet — the triage payload (2-line clamp) */}
      {job.snippet && (
        <Text variant='meta' className='line-clamp-2 text-text-secondary'>
          {job.snippet}
        </Text>
      )}
      {/* meta row: salary + posted, pinned to the bottom so cards line up */}
      <div className='mt-auto flex items-baseline justify-between gap-3'>
        <Text
          variant='meta'
          className={
            job.salary_text
              ? 'font-medium text-text-primary'
              : 'text-text-tertiary'
          }
        >
          {job.salary_text || 'Salary not listed'}
        </Text>
        <Text variant='meta' className='shrink-0 text-text-tertiary'>
          {timeAgo(job.created_at)}
        </Text>
      </div>
      {/* footer: pipeline-state (§11.1), LOGGED-IN ONLY. Bound → the "✓ In
          <target>" badge; otherwise a quiet "Add to target" affordance. Both are
          presentational — the card itself opens the modal, where the action
          happens. Logged-out renders no footer: a pure click-target (§11.1). */}
      {isAuthenticated && (
        <div className='flex items-center gap-2 border-t border-border pt-3'>
          {bound ? (
            <Badge variant='brand' className='max-w-full gap-1'>
              <Check className='size-3.5 shrink-0' aria-hidden />
              <span className='truncate'>In “{inTargets[0].label}”</span>
            </Badge>
          ) : (
            <Badge variant='default' className='gap-1'>
              <Plus className='size-3.5 shrink-0' aria-hidden />
              Add to target
            </Badge>
          )}
        </div>
      )}
    </Link>
  );
}

export default function JobSearchExplorer({
  isAuthenticated,
}: {
  /** From the page's server-side `getUser()` (#467 §10). Logged-out: fetch the
   *  public BFF route, no membership, no per-card footer, and a soft signup
   *  allusion in the detail. Authed: the full bind→unlock experience, unchanged. */
  isAuthenticated: boolean;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  // The search BFF differs by audience: the authed route proxies the caller's
  // JWT (`/api/jobs/search`); the public route is the unauth, BFF-secret-gated,
  // depth-capped forwarder (`/api/public/search`). Same response shape, so only
  // the URL changes.
  const searchEndpoint = isAuthenticated
    ? '/api/jobs/search'
    : '/api/public/search';

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

  // Target-membership for the visible page (#467 §11): which of the caller's
  // targets already contain each listing → the card's pipeline-state badge and
  // the detail modal's bound/unbound split. Best-effort; a failure just leaves
  // the map empty and NEVER blocks search. Keyed by job id.
  const [membershipByJob, setMembershipByJob] = useState<
    Record<string, TargetRef[]>
  >({});

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
      const res = await fetch(`${searchEndpoint}?${sp.toString()}`);
      if (!res.ok) throw new Error(await extractApiError(res, 'Search failed'));
      return (await res.json()) as JobSearchResponse;
    },
    [searchEndpoint]
  );

  // Fetch target-membership for a batch of results and merge it into the map
  // (#467 §11). Strictly best-effort: any failure (auth, network, non-OK) is
  // swallowed so the badge/modal simply fall back to "unbound" — search must
  // never depend on this. Runs after each page lands (page 0 + Load more).
  const fetchMembership = useCallback(
    async (jobs: JobSearchResult[]) => {
      // Membership is a per-user concept — never fetched on the logged-out
      // surface (no session, and the card/detail render no membership UI).
      if (!isAuthenticated) return;
      const ids = jobs.map(j => j.id);
      if (ids.length === 0) return;
      try {
        const res = await fetch('/api/jobs/target-membership', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ job_posting_ids: ids }),
        });
        if (!res.ok) return;
        const data = (await res.json()) as TargetMembershipResponse;
        if (data.memberships) {
          setMembershipByJob(prev => ({ ...prev, ...data.memberships }));
        }
      } catch {
        // best-effort — leave the map as-is
      }
    },
    [isAuthenticated]
  );

  // Run the search whenever the URL search-state changes: initial mount, a
  // submit/filter change (which commits to the URL), or back/forward. This is
  // the ONLY place a fresh (page-0) search is kicked, so the URL and the results
  // never drift. An empty query renders the honest empty page.
  useEffect(() => {
    setDraftQ(urlQ);
    setDraftLocation(urlLocation);
    // A fresh search invalidates the previous page's membership map.
    setMembershipByJob({});
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
        void fetchMembership(data.results); // best-effort, non-blocking
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
  }, [urlQ, urlLocation, urlDays, fetchPage, fetchMembership]);

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
      void fetchMembership(data.results); // best-effort, non-blocking
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Network error loading more.');
    } finally {
      setLoadingMore(false);
    }
  }, [fetchPage, fetchMembership, urlQ, urlLocation, urlDays, results]);

  const hasActiveFilters = Boolean(urlLocation.trim() || urlDays);
  const onEnter = (e: ReactKeyboardEvent) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      submitSearch();
    }
  };

  return (
    // Page shell matches the other (app) pages (Jobs / Targets): a full-width
    // `flex flex-col gap-6` column with a hero `Heading` — not a centered
    // max-w-3xl block with a bespoke text-2xl h1.
    <div className='flex flex-col gap-6'>
      <div>
        <Heading variant='hero' as='h1'>
          Search jobs
        </Heading>
        {/* Sub-copy differs by audience. Authed is UNCHANGED (steers to Jobs for
            the AI match); logged-out is the honest "no account needed" framing —
            the conversion moment is deferred to the detail's soft allusion. */}
        {isAuthenticated ? (
          <Text variant='body' className='mt-1 text-text-secondary'>
            Browse the full job pool by keyword. These results aren’t scored
            against your profile — head to{' '}
            <Link href='/jobs' className='underline underline-offset-2'>
              Jobs
            </Link>{' '}
            to see how you match.
          </Text>
        ) : (
          <Text variant='body' className='mt-1 text-text-secondary'>
            Browse the full job pool by keyword — no account needed. Open any
            role for the details and a link to the original posting.
          </Text>
        )}
      </div>

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
          {/* Shared-ui Select — same design-system control (and default size)
              as the Input above, so the two line up without hand-tuned heights.
              aria-label instead of a visible label to keep the row compact. */}
          <div className='w-44'>
            <Select
              id='job-search-recency'
              aria-label='Filter by date posted'
              value={urlDays}
              onChange={e => changeRecency(e.target.value)}
              options={RECENCY_OPTIONS}
            />
          </div>
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
              <div className='grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3'>
                {results.map(job => (
                  <JobSearchCard
                    key={job.id}
                    job={job}
                    inTargets={membershipByJob[job.id] ?? []}
                    isAuthenticated={isAuthenticated}
                  />
                ))}
              </div>
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

      {/* The listing detail itself no longer renders here (#467 §11.2
          fast-follow): a card click soft-navigates to `/search/<id>`, and the
          layout's `@modal` slot renders the intercepted detail OVER this grid
          (which stays mounted — state, scroll and all). ListingModal owns the
          fetch, membership and the bind→unlock live flip. */}
    </div>
  );
}
