'use client';

import {
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  useCallback,
  useEffect,
  useState,
} from 'react';
import Link from 'next/link';
import { formatJobSalary } from '@/lib/formatSalary';
import { displayTitle } from '@/lib/displayTitle';
import { formatCompanyName } from '@/lib/formatCompanyName';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { Check, ChevronDown } from 'lucide-react';
import { Avatar } from '@danieljoffe/shared-ui/Avatar';
import { Badge } from '@danieljoffe/shared-ui/Badge';
import type { ButtonVariant } from '@danieljoffe/shared-ui/Button';
import { Heading } from '@danieljoffe/shared-ui/Heading';
import { Input } from '@danieljoffe/shared-ui/Input';
import {
  Dropdown,
  type DropdownItem,
  type DropdownTriggerProps,
} from '@danieljoffe/shared-ui/Dropdown';
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
// The public surface's maximum reachable depth: PUBLIC_MAX_OFFSET (40) + one
// page (public_search.py's anti-enumeration cap). Used only to decide when
// the end-of-results line should pitch signing in rather than say nothing —
// if the caps drift apart the line simply stops appearing; nothing breaks.
const PUBLIC_RESULT_DEPTH = 60;

/** Recency filter presets. The value is the `posted_within` day-count carried in
 *  the URL + sent to the API (`''` = any time). */
const RECENCY_OPTIONS: SelectOption[] = [
  { value: '', label: 'Any time' },
  { value: '7', label: 'Past week' },
  { value: '30', label: 'Past month' },
  { value: '90', label: 'Past 3 months' },
];

/** Salary-floor presets (yearly USD). The value is the `salary_min` carried in
 *  the URL and sent to the API as `salary_floor` (`''` = any salary). Rows whose
 *  posted yearly-USD range reaches the floor pass; roles without a posted
 *  salary are excluded while a floor is active. */
const SALARY_OPTIONS: SelectOption[] = [
  { value: '', label: 'Any salary' },
  { value: '100000', label: '$100k+' },
  { value: '150000', label: '$150k+' },
  { value: '200000', label: '$200k+' },
  { value: '250000', label: '$250k+' },
  { value: '300000', label: '$300k+' },
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

/** Ordered logo-link tiers for a company domain (#470) — links only, per
 *  the owner's constraint; nothing is stored. Brandfetch leads WHEN a client
 *  id is configured (NEXT_PUBLIC_BRANDFETCH_CLIENT_ID — inlined at build
 *  time), DuckDuckGo's token-free favicon follows, and the caller falls back
 *  to the initials monogram when every tier errors.
 *
 *  Every host here is a third party that receives the visitor's IP and the
 *  company domain being viewed, so the list is deliberately short and bound
 *  to the privacy policy by the `legalPages` spec — see `logoImageOrigins`
 *  for why Google's favicon endpoint is not among them. */
export function logoUrlTiers(domain: string): string[] {
  const tiers: string[] = [];
  const clientId = process.env.NEXT_PUBLIC_BRANDFETCH_CLIENT_ID;
  if (clientId) {
    tiers.push(
      `https://cdn.brandfetch.io/${encodeURIComponent(domain)}?c=${encodeURIComponent(clientId)}`
    );
  }
  tiers.push(
    `https://icons.duckduckgo.com/ip3/${encodeURIComponent(domain)}.ico`
  );
  return tiers;
}

export function CompanyAvatar({
  name,
  domain,
  size = 'md',
}: {
  name: string;
  /** Verified company domain (#470); absent → initials monogram, as ever. */
  domain?: string | null | undefined;
  /** `md` on the card grid, `lg` in the detail modal header (#467 §11.2). */
  size?: 'md' | 'lg';
}) {
  // Stateless dispatcher. The cascade's tier lives in the child, KEYED BY
  // DOMAIN, so a new company remounts it at tier 0.
  //
  // A key on the <img> alone does NOT do this: it remounts the image while
  // leaving this component's state intact, so the next company would resume
  // at the previous one's failed tier — or, once a cascade had been
  // exhausted, render initials without ever requesting its logo. That
  // matters wherever one instance sees successive companies: the detail
  // modal feeds changing job props into the same subtree.
  if (!domain) return <CompanyInitialsAvatar name={name} size={size} />;
  return <CompanyLogo key={domain} name={name} domain={domain} size={size} />;
}

/** The cascade itself: walk `logoUrlTiers` on each image error, then fall
 *  back to the initials monogram. Mounted per domain (see above), so its
 *  tier state is scoped to one company by construction. */
function CompanyLogo({
  name,
  domain,
  size,
}: {
  name: string;
  domain: string;
  size: 'md' | 'lg';
}) {
  const [tier, setTier] = useState(0);
  const tiers = logoUrlTiers(domain);
  if (tier >= tiers.length) {
    return <CompanyInitialsAvatar name={name} size={size} />;
  }
  const px = size === 'lg' ? 48 : 40;
  return (
    <img
      src={tiers[tier]}
      alt=''
      aria-hidden
      width={px}
      height={px}
      loading='lazy'
      referrerPolicy='no-referrer'
      className='shrink-0 rounded-md object-contain bg-white'
      onError={() => setTier(t => t + 1)}
    />
  );
}

function CompanyInitialsAvatar({
  name,
  size = 'md',
}: {
  name: string;
  size?: 'md' | 'lg';
}) {
  // Shared-ui Avatar (square) carrying the deterministic per-company hue. Avatar
  // exposes no per-instance colour prop — only a static `tileClassName` — so the
  // computed hsl() rides a CSS variable set on the root and consumed by the inner
  // tile via an arbitrary-value background class (see tileClassName below).
  // Solid colour + white text stays self-contained and reads in light and dark
  // themes. (Don't quote the class shape with a "..." placeholder here — the
  // Tailwind scanner token-matches comments too, and the placeholder compiles
  // to an invalid `var(...)` utility that 500s `next dev`.)
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
 *
 * Composed on shared-ui Dropdown (#485, 0.10+ composable trigger): the
 * primitive owns open state, outside-click/Escape dismiss, focus return, and
 * menu keyboard nav; we keep the design-system Button trigger via the
 * render-function `trigger` and stay CONTROLLED so the menu can lazily load
 * targets on open, stay open through the async pick, and close on success.
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
  const { targets, loading, error, ensureLoaded } = source;

  const handleOpenChange = (next: boolean) => {
    setOpen(next);
    if (next) ensureLoaded(); // lazy fetch on first open
  };

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

  // Whole-menu loading / error / empty states as disabled status items — the
  // documented Dropdown idiom for async menus.
  const items: DropdownItem[] = loading
    ? [
        {
          label: 'Loading targets…',
          disabled: true,
          content: (
            <span className='flex items-center gap-2' role='status'>
              <Spinner size='sm' aria-label='Loading targets' />
              <Text variant='meta'>Loading targets…</Text>
            </span>
          ),
        },
      ]
    : error
      ? [
          {
            label: error,
            disabled: true,
            content: (
              <Text variant='error' role='alert'>
                {error}
              </Text>
            ),
          },
        ]
      : !targets || targets.length === 0
        ? [
            {
              label: 'No targets yet — create one first.',
              disabled: true,
            },
          ]
        : targets.map(t => ({
            label: pendingId === t.id ? 'Adding…' : t.label,
            onClick: () => void pick(t),
            disabled: pendingId !== null && pendingId !== t.id,
            loading: pendingId === t.id,
            // The pick is async: keep the menu open while pending; success
            // closes it explicitly via the controlled state above.
            closeOnClick: false,
            // Every item carries its full label as a native tooltip — long
            // labels truncate in the fixed-width menu with no way to read
            // them (re-sweep R6). Inactive targets stay pickable (adding
            // still scores the job) but say so — the bare list read as 7
            // equal targets while Jobs shows 2 active tabs (§B2).
            ...(pendingId === t.id
              ? {}
              : {
                  content: (
                    <span
                      className='flex items-baseline gap-1.5'
                      title={t.label}
                    >
                      <span className='min-w-0 truncate'>{t.label}</span>
                      {!t.isActive && (
                        <Text
                          variant='meta'
                          as='span'
                          className='shrink-0 text-text-tertiary'
                        >
                          inactive
                        </Text>
                      )}
                    </span>
                  ),
                }),
          }));

  // Escape guard (revised for shared-ui 0.12): the menu now renders in a
  // document.body PORTAL, so a keydown with focus inside it bubbles
  // panel → body → document → window without ever passing through this
  // component's DOM — the 0.11 wrapper-listener guard is no longer in the
  // path. shared-ui Modal still listens for Escape on window (and doesn't
  // check ``defaultPrevented``), so the keystroke that closes the menu
  // would close a hosting detail modal too. A document-level bubble
  // listener sits between the portal and window in every path (trigger-
  // focused Escapes bubble through document as well) and swallows exactly
  // the Escapes the menu CONSUMED: the primitive calls preventDefault()
  // before closing, so ``defaultPrevented`` marks them.
  //
  // LIFETIME attachment ([] deps), deliberately NOT gated on ``open``:
  // keydown is a DISCRETE event, so when the primitive's panel listener
  // closes the menu, React flushes the state update AND passive-effect
  // cleanup synchronously mid-dispatch — an ``open``-gated guard is
  // removed from document before the very Escape it exists to swallow
  // arrives there (verified with the real browser via
  // authed-search-target-picker.spec.ts; jsdom does not reproduce the
  // mid-dispatch flush). Same race the pre-0.12 comment warned about,
  // one node higher. The ``defaultPrevented`` predicate keeps the
  // always-on listener scoped to consumed Escapes.
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && e.defaultPrevented) e.stopPropagation();
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, []);

  return (
    <div>
      <Dropdown
        open={open}
        onOpenChange={handleOpenChange}
        items={items}
        panelClassName='max-h-64 w-64 max-w-[calc(100vw-2rem)] overflow-auto'
        trigger={(triggerProps: DropdownTriggerProps) => (
          <Button
            name='search-add-to-target'
            variant={variant}
            size='sm'
            {...triggerProps}
          >
            {label}
            <ChevronDown className='ml-1 size-3.5 opacity-70' aria-hidden />
          </Button>
        )}
      />
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
  const meta = [formatCompanyName(job.company_name), formatLocation(job)]
    .filter(Boolean)
    .join(' · ');
  const bound = inTargets.length > 0;

  return (
    <Link
      href={`/search/${job.id}`}
      scroll={false}
      aria-label={`Open ${displayTitle(job)} at ${job.company_name}`}
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
        <CompanyAvatar
          name={formatCompanyName(job.company_name)}
          domain={job.company_domain}
        />
        <div className='min-w-0 flex-1'>
          <span className='font-semibold transition-colors group-hover:text-text-brand'>
            {displayTitle(job)}
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
            formatJobSalary(job)
              ? 'font-medium text-text-primary'
              : 'text-text-tertiary'
          }
        >
          {formatJobSalary(job) || 'Salary not listed'}
        </Text>
        <Text variant='meta' className='shrink-0 text-text-tertiary'>
          {timeAgo(job.source_posted_at ?? job.cataloged_at)}
        </Text>
      </div>
      {/* footer: pipeline-state (§11.1), LOGGED-IN ONLY. Both badges are
          presentational — the card itself opens the modal, where the action
          happens. The unbound state reads as STATE ("Not in a target"), not
          as a control: the old "+ Add to target" badge was pixel-identical
          to the modal's real button and clicking it did nothing (#836 §4).
          Logged-out renders no footer: a pure click-target (§11.1). */}
      {isAuthenticated && (
        <div className='flex items-center gap-2 border-t border-border pt-3'>
          {bound ? (
            <Badge variant='brand' className='max-w-full gap-1'>
              <Check className='size-3.5 shrink-0' aria-hidden />
              <span className='truncate'>In “{inTargets[0].label}”</span>
            </Badge>
          ) : (
            <Badge variant='default' className='text-text-tertiary'>
              Not in a target
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
  const urlSalary = searchParams.get('salary_min') ?? '';

  // Text-input drafts, seeded from the URL and re-synced whenever it changes
  // underneath us (back/forward). Submitting commits them back to the URL.
  const [draftQ, setDraftQ] = useState(urlQ);
  const [draftLocation, setDraftLocation] = useState(urlLocation);

  const [results, setResults] = useState<JobSearchResult[] | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false); // fresh search
  const [loadingMore, setLoadingMore] = useState(false); // "Load more"
  const [error, setError] = useState<string | null>(null);
  // Pagination failures get their OWN error state: the page-level `error`
  // gates the whole results section, so routing a "Load more" failure into
  // it unmounted every result already on screen — a transient blip at the
  // 4th page wiped the user's whole session of scrolling (#832). This one
  // renders inline next to the button; the loaded results stay put.
  const [loadMoreError, setLoadMoreError] = useState<string | null>(null);

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
      salary: string;
      offset: number;
    }) => {
      const sp = new URLSearchParams({
        q: opts.q,
        page_size: String(PAGE_SIZE),
        offset: String(opts.offset),
      });
      if (opts.location.trim()) sp.set('location', opts.location.trim());
      if (opts.days) sp.set('posted_within_days', opts.days);
      if (opts.salary) sp.set('salary_floor', opts.salary);
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
  // never drift. An empty query BROWSES the pool newest-first (#834) — bare
  // /search shows the corpus instead of a blank page, and filters work with
  // no keyword ("remote, past week, $150k+" is a legitimate first ask).
  useEffect(() => {
    setDraftQ(urlQ);
    setDraftLocation(urlLocation);
    // A fresh search invalidates the previous page's membership map.
    setMembershipByJob({});
    let cancelled = false;
    setLoading(true);
    setError(null);
    setLoadMoreError(null);
    setResults(null);
    fetchPage({
      q: urlQ,
      location: urlLocation,
      days: urlDays,
      salary: urlSalary,
      offset: 0,
    })
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
  }, [urlQ, urlLocation, urlDays, urlSalary, fetchPage, fetchMembership]);

  // Commit search state to the URL (replace → the /search entry updates in place,
  // so "back" from a listing lands on this exact query+filters rather than a
  // stack of every keystroke). The effect above re-runs the search off the new
  // URL. `scroll:false` keeps the viewport put on a filter tweak.
  const commit = useCallback(
    (next: { q: string; location: string; days: string; salary: string }) => {
      const params = new URLSearchParams();
      if (next.q.trim()) params.set('q', next.q.trim());
      if (next.location.trim()) params.set('location', next.location.trim());
      if (next.days) params.set('posted_within', next.days);
      if (next.salary) params.set('salary_min', next.salary);
      const qs = params.toString();
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    },
    [router, pathname]
  );

  const submitSearch = () =>
    commit({
      q: draftQ,
      location: draftLocation,
      days: urlDays,
      salary: urlSalary,
    });
  const changeRecency = (days: string) =>
    commit({ q: draftQ, location: draftLocation, days, salary: urlSalary });
  const changeSalary = (salary: string) =>
    commit({ q: draftQ, location: draftLocation, days: urlDays, salary });
  const clearFilters = () =>
    commit({ q: draftQ, location: '', days: '', salary: '' });

  const loadMore = useCallback(async () => {
    if (!results) return;
    setLoadingMore(true);
    setLoadMoreError(null);
    try {
      const data = await fetchPage({
        q: urlQ,
        location: urlLocation,
        days: urlDays,
        salary: urlSalary,
        offset: results.length,
      });
      setResults(prev => [...(prev ?? []), ...data.results]);
      setHasMore(data.has_more);
      void fetchMembership(data.results); // best-effort, non-blocking
    } catch (e) {
      // NEVER the page-level `error` — that unmounts the loaded results
      // (#832). The button stays rendered, so clicking again is the retry.
      setLoadMoreError(
        e instanceof Error ? e.message : 'Network error loading more.'
      );
    } finally {
      setLoadingMore(false);
    }
  }, [
    fetchPage,
    fetchMembership,
    urlQ,
    urlLocation,
    urlDays,
    urlSalary,
    results,
  ]);

  const hasActiveFilters = Boolean(urlLocation.trim() || urlDays || urlSalary);
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
            role for a preview and a link to the original posting.
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
            disabled={loading}
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
          {/* Salary floor — same Select control as recency; yearly-USD
              semantics live in the API (roles without a posted salary are
              excluded while a floor is active). */}
          <div className='w-36'>
            <Select
              id='job-search-salary'
              aria-label='Filter by minimum salary'
              value={urlSalary}
              onChange={e => changeSalary(e.target.value)}
              options={SALARY_OPTIONS}
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
                {urlQ
                  ? `No roles match “${urlQ}”${hasActiveFilters ? ' with these filters' : ''} yet.`
                  : 'No roles match these filters yet.'}
              </Text>
              <Text variant='meta' className='text-text-secondary'>
                {hasActiveFilters
                  ? 'Try widening the location or date range, or a broader title.'
                  : 'We’re expanding coverage — try a broader title, or check back soon.'}
              </Text>
            </div>
          ) : (
            <>
              {/* Honest count (#836): the API deliberately computes no corpus
                  total (a title-ranked search would need a COUNT query), so
                  say what we DO know — whether this is everything or just
                  the first page(s) of more. */}
              <Text variant='meta' as='p' className='mb-3 text-text-secondary'>
                {hasMore
                  ? `Showing the first ${results.length} ${urlQ ? 'matches' : 'roles'}`
                  : urlQ
                    ? `${results.length} ${results.length === 1 ? 'match' : 'matches'}`
                    : `${results.length} roles, newest first`}
              </Text>
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
                <div className='mt-4 flex flex-col items-center gap-2'>
                  <Button
                    name='job-search-load-more'
                    variant='secondary'
                    onClick={loadMore}
                    disabled={loadingMore}
                    aria-busy={loadingMore}
                  >
                    {loadingMore ? 'Loading…' : 'Load more'}
                  </Button>
                  {loadMoreError && !loadingMore && (
                    <Text variant='error' role='alert'>
                      {loadMoreError} — your results are still here; try again.
                    </Text>
                  )}
                </div>
              )}
              {/* The public API stops pagination at its anti-enumeration
                  ceiling (offset ≤ 40 → 60 rows), and now reports
                  has_more=false there (#832). Say so honestly — and make
                  it the conversion moment: signing in searches the full
                  pool (the authed window pages ~4× deeper). */}
              {!hasMore &&
                !isAuthenticated &&
                results.length >= PUBLIC_RESULT_DEPTH && (
                  <Text
                    variant='meta'
                    as='p'
                    className='mt-4 text-center text-text-secondary'
                  >
                    That’s the first {PUBLIC_RESULT_DEPTH} —{' '}
                    <Link
                      href='/login'
                      className='underline underline-offset-2'
                    >
                      sign in
                    </Link>{' '}
                    to search the full pool.
                  </Text>
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
