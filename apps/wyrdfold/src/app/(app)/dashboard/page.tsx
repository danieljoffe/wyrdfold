import type { Metadata } from 'next';
import { redirect } from 'next/navigation';
import * as Sentry from '@sentry/nextjs';
import { Heading } from '@danieljoffe/shared-ui/Heading';
import { Text } from '@danieljoffe/shared-ui/Text';
import { fetchJsonFromWyrdfoldAPI } from '@/lib/api/proxy';
import type { InsightsInitial } from '@/hooks/useInsights';
import DashboardPage, { type DashboardInitial } from '../DashboardPage';
import InsightsDashboard from '../insights/InsightsDashboard';
import type {
  Period,
  PipelineInsights,
  SkillsCostInsights,
  TargetInsights,
} from '../insights/types';
import type { JobPosting } from '../jobs/types';
import { hasProse, type ProseResponse } from '../profile/types';
import type { UserTargetWithSummary } from '../targets/types';
import HomeViewToggle from './HomeViewToggle';
import { parseHomeView } from './view';

interface OnboardingStatus {
  completed_at: string | null;
  path: 'A' | 'B' | 'C' | null;
  current_step: string | null;
}

export const metadata: Metadata = {
  title: 'Home',
};

interface JobsListResponse {
  postings: JobPosting[];
  // Cursor pagination (#113): total is best-effort (null on the keyset path),
  // next_cursor drives "load more". The dashboard only reads ``postings``.
  total: number | null;
  next_cursor: string | null;
}

// Mirrors ``PIPELINE_STATS`` in ``DashboardPage.tsx`` — keep in sync so
// every counter shown on the dashboard has a backing fetch here.
const PIPELINE_STATUSES = [
  'new',
  'saved',
  'resume_draft',
  'resume_ready',
  'applied',
  'interviewing',
  'offer',
] as const;

const DEFAULT_PERIOD: Period = '30d';

/** The Today section's server-seeded data (the daily launcher).
 *  Takes the in-flight onboarding read (rather than its resolved boolean) so
 *  the widget fetches overlap it instead of queuing behind it — the gate
 *  costs a full API round-trip, and this render blocks the dashboard's
 *  content paint. ``confirmedOnboarded`` gates the flag-set-but-no-prose
 *  telemetry: on the fail-open path (degraded onboarding read) we don't
 *  actually know the flag state, and the same degraded API likely returns no
 *  prose too — firing there would be misleading. */
async function fetchTodayInitial(
  onboarding: Promise<OnboardingStatus | null>
): Promise<DashboardInitial> {
  // ``hasProfile`` checks whether the user has authored prose at all —
  // the underlying signal that "they've started onboarding." The
  // upstream ``/experience/prose`` endpoint returns ``{prose: null}``
  // when empty and the bare ``ProseDoc`` (``{id, content, ...}``) when
  // populated — two shapes for the same response. ``hasProse`` is the
  // type-guard from ``profile/types`` that handles both correctly.
  // A naive ``proseRes?.prose != null`` check matched only the empty
  // case and silently treated every populated response as ``no profile``,
  // sending users back to the onboarding CTA after they'd onboarded.
  const [topRes, proseRes, targetsRes, countsRes, onboardingStatus] =
    await Promise.all([
      fetchJsonFromWyrdfoldAPI<JobsListResponse>('/jobs', {
        searchParams: new URLSearchParams({
          status: 'new',
          sort: 'score',
          order: 'desc',
          page_size: '5',
        }),
      }),
      fetchJsonFromWyrdfoldAPI<ProseResponse>('/experience/prose'),
      fetchJsonFromWyrdfoldAPI<{ targets: UserTargetWithSummary[] }>(
        '/targets/mine'
      ),
      fetchJsonFromWyrdfoldAPI<Record<string, number>>('/jobs/pipeline-counts'),
      onboarding,
    ]);
  const confirmedOnboarded = onboardingStatus?.completed_at != null;

  // Flag is set but prose isn't there. Causes: a Path A/B user who
  // skipped the resume step (or whose upload failed mid-flow — see
  // the OpenRouter 402 incident), or data drift from a support action.
  // We still surface to Sentry so we notice the latter, but we no
  // longer bounce back to /onboarding: the OnboardingWizard always
  // restarts at ``path-chooser``, which means any user who lands here
  // legitimately (resume skipped) gets trapped in a redirect loop.
  // DashboardPage's ``!hasProfile`` branch already renders a graceful
  // empty state with a "Set up profile" CTA, which is what the user
  // actually needs.
  const proseAuthored = proseRes != null && hasProse(proseRes);
  if (confirmedOnboarded && !proseAuthored) {
    Sentry.captureMessage('dashboard:onboarding_flag_set_but_no_prose', {
      level: 'warning',
    });
  }

  const counts: Record<string, number> = {};
  for (const status of PIPELINE_STATUSES) {
    counts[status] = countsRes?.[status] ?? 0;
  }

  return {
    topMatches: topRes?.postings ?? [],
    counts,
    hasProfile: proseAuthored,
    hasActiveTargets:
      targetsRes?.targets?.some(t => t.user_target.is_active) ?? false,
  };
}

/** The Trends section's server-seeded data (the former /insights page).
 *  Fetched in parallel so the section paints with data instead of three
 *  client→Next→API round-trips after hydration (#851 P1). Each slice is
 *  null on failure so a partial outage still renders what came back. */
async function fetchTrendsInitial(): Promise<InsightsInitial> {
  const qs = new URLSearchParams({ period: DEFAULT_PERIOD });
  const [pipeline, targets, skillsCost] = await Promise.all([
    fetchJsonFromWyrdfoldAPI<PipelineInsights>('/insights/pipeline', {
      searchParams: qs,
    }),
    fetchJsonFromWyrdfoldAPI<TargetInsights>('/insights/targets', {
      searchParams: qs,
    }),
    fetchJsonFromWyrdfoldAPI<SkillsCostInsights>('/insights/skills-cost', {
      searchParams: qs,
    }),
  ]);

  return {
    period: DEFAULT_PERIOD,
    pipeline: pipeline ?? undefined,
    targets: targets ?? undefined,
    skillsCost: skillsCost ?? undefined,
    fetchedAt: Date.now(),
  };
}

export default async function WyrdfoldHome({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  // Primary gate: explicit onboarding_completed_at flag on user_profiles.
  // A NULL flag = user hasn't finished the wizard → redirect to it.
  // See plan-wyrdfold-onboarding-completion-tracking.md.
  //
  // Redirect ONLY on a confirmed "not onboarded": a status object whose
  // completed_at is null. ``fetchJsonFromWyrdfoldAPI`` returns ``null`` on
  // any read failure (transient auth refresh race, network blip, upstream
  // 5xx). The old ``onboardingStatus?.completed_at == null`` collapsed that
  // failure case into "never onboarded" and redirected — so a single flaky
  // read bounced an *already-onboarded* user back into the wizard (and,
  // because the wizard restarts at path-chooser, into a redirect loop).
  // Failing open here is safe: DashboardPage renders a graceful setup CTA
  // for a genuinely-new user, so the worst case for an un-onboarded user
  // whose read failed is they see the dashboard's empty state instead of
  // the wizard for one load.
  const onboardingPromise = fetchJsonFromWyrdfoldAPI<OnboardingStatus>(
    '/profile/onboarding'
  );
  const params = await searchParams;

  // Home = daily launcher ("Today") + historical view ("Trends", the
  // former /insights page — UX/IA Fork A). One section per request:
  // the server fetches only the data the selected view renders.
  //
  // The section's fetches start BEFORE the onboarding gate resolves — the
  // gate is one API round-trip, and serializing it ahead of the widgets
  // added that latency to every dashboard paint. The trade: a
  // not-yet-onboarded user's single pre-wizard visit now fires the widget
  // reads too, wasted when the redirect below aborts the render. That's a
  // handful of one-time reads on the rarest path vs. a round-trip saved on
  // every load of the app's default page.
  const view = parseHomeView(params['view']);
  const todayPromise =
    view === 'today' ? fetchTodayInitial(onboardingPromise) : null;
  const trendsPromise = view === 'trends' ? fetchTrendsInitial() : null;

  const onboardingStatus = await onboardingPromise;
  if (onboardingStatus !== null && onboardingStatus.completed_at == null) {
    redirect('/onboarding');
  }

  const todayInitial = todayPromise ? await todayPromise : null;
  const trendsInitial = trendsPromise ? await trendsPromise : null;

  return (
    <div className='flex flex-col gap-6'>
      <div className='flex flex-wrap items-end justify-between gap-4'>
        <div>
          <Heading variant='hero' as='h1'>
            Home
          </Heading>
          <Text variant='body' className='mt-1 text-text-secondary'>
            {view === 'trends'
              ? 'Track your job search progress'
              : 'Your job search at a glance'}
          </Text>
        </div>
        <HomeViewToggle value={view} />
      </div>

      {view === 'trends' ? (
        <InsightsDashboard
          {...(trendsInitial ? { initial: trendsInitial } : {})}
        />
      ) : (
        todayInitial && <DashboardPage initial={todayInitial} />
      )}
    </div>
  );
}
