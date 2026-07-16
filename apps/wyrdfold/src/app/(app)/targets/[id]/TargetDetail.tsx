'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import Link from 'next/link';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { Badge } from '@danieljoffe/shared-ui/Badge';
import { Heading } from '@danieljoffe/shared-ui/Heading';
import { Skeleton } from '@danieljoffe/shared-ui/Skeleton';
import { Tabs, type Tab } from '@danieljoffe/shared-ui/Tabs';
import { Text } from '@danieljoffe/shared-ui/Text';
import Breadcrumbs, { crumbLabel } from '@/components/kit/Breadcrumbs';
import { useToast } from '@/state/Toast/ToastProvider';
import type {
  JobTarget,
  TargetReferenceJD,
  UserTarget,
  UserTargetWithTarget,
} from '../types';
import AxisWeightsEditor from './AxisWeightsEditor';
import LearningLogPanel from './LearningLogPanel';
import NotificationThresholdsEditor from './NotificationThresholdsEditor';
import ReferenceJDList from './ReferenceJDList';
import ScoringProfileView from './ScoringProfileView';
import TargetDetailSkeleton from './TargetDetailSkeleton';
import TargetPreferencesEditor from './TargetPreferencesEditor';

interface TargetDetailProps {
  id: string;
}

/**
 * Target-detail tab set (UX/IA Forks B+C). The former nine stacked cards
 * become four tabs behind a persistent header; only the active tab's
 * editors mount, so their GETs load lazily per tab. ``?tab=`` keeps the
 * choice shareable and sticky across reloads (``router.replace`` — tab
 * flips shouldn't pile up history entries).
 */
const TAB_IDS = ['scoring', 'preferences', 'jds', 'learning'] as const;
type TabId = (typeof TAB_IDS)[number];

function parseTab(raw: string | null): TabId {
  return (TAB_IDS as readonly string[]).includes(raw ?? '')
    ? (raw as TabId)
    : 'scoring';
}

export default function TargetDetail({ id }: TargetDetailProps) {
  const [target, setTarget] = useState<JobTarget | null>(null);
  const [userTarget, setUserTarget] = useState<UserTarget | null>(null);
  const [referenceJDs, setReferenceJDs] = useState<TargetReferenceJD[]>([]);
  const [jdsLoaded, setJdsLoaded] = useState(false);
  const [loading, setLoading] = useState(true);
  const { toast } = useToast();

  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const [activeTab, setActiveTab] = useState<TabId>(() =>
    parseTab(searchParams.get('tab'))
  );
  // The shared Tabs component is uncontrolled; remember what the URL said
  // on first render so the two never disagree about the initial tab.
  const initialTabRef = useRef(activeTab);

  const handleTabChange = useCallback(
    (tabId: string) => {
      const tab = parseTab(tabId);
      setActiveTab(tab);
      const params = new URLSearchParams(searchParams.toString());
      if (tab === 'scoring') {
        params.delete('tab');
      } else {
        params.set('tab', tab);
      }
      const qs = params.toString();
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    },
    [pathname, router, searchParams]
  );

  const fetchTarget = useCallback(async () => {
    try {
      const res = await fetch(`/api/targets/${id}`);
      if (!res.ok) throw new Error('Failed to fetch target');
      const data = (await res.json()) as JobTarget;
      setTarget(data);
    } catch {
      toast({ variant: 'error', title: 'Failed to load target' });
    }
  }, [id, toast]);

  /**
   * Fetch this user's `user_target` row for the current target. Powers
   * the axis-weights editor (which reads from `user_targets`, not the
   * shared `targets` row) and the header's fit chip. One round-trip via
   * the per-target endpoint — supersedes the old fetch-all-/mine pattern.
   */
  const fetchUserTarget = useCallback(async () => {
    try {
      const res = await fetch(`/api/targets/${id}/user-target`);
      if (!res.ok) throw new Error('Failed to fetch user target');
      const payload = (await res.json()) as UserTargetWithTarget;
      setUserTarget(payload.user_target);
    } catch {
      // Non-fatal — the axis weights editor will just not render.
      // The rest of the page still works.
    }
  }, [id]);

  const fetchReferenceJDs = useCallback(async () => {
    try {
      const res = await fetch(`/api/targets/${id}/reference-jds`);
      if (!res.ok) throw new Error('Failed to fetch reference JDs');
      const payload = (await res.json()) as {
        reference_jds: TargetReferenceJD[];
      };
      setReferenceJDs(payload.reference_jds);
      setJdsLoaded(true);
    } catch {
      toast({ variant: 'error', title: 'Failed to load reference JDs' });
    }
  }, [id, toast]);

  // Header + Scoring tab data on mount; everything else loads with its
  // tab (the editors behind the other tabs fetch their own data when
  // they mount, and reference JDs fetch on first tab activation below).
  useEffect(() => {
    let cancelled = false;

    Promise.all([fetchTarget(), fetchUserTarget()]).finally(() => {
      if (!cancelled) setLoading(false);
    });

    return () => {
      cancelled = true;
    };
  }, [fetchTarget, fetchUserTarget]);

  // Lazy-load the reference JDs the first time their tab opens (or on
  // mount when the URL deep-links straight to it).
  useEffect(() => {
    if (activeTab === 'jds' && !jdsLoaded) {
      fetchReferenceJDs();
    }
  }, [activeTab, jdsLoaded, fetchReferenceJDs]);

  const handleRefresh = useCallback(() => {
    fetchTarget();
    fetchReferenceJDs();
  }, [fetchTarget, fetchReferenceJDs]);

  const tabs: Tab[] = useMemo(() => {
    if (!target) return [];
    return [
      {
        id: 'scoring',
        label: 'Scoring',
        content: (
          <div className='flex flex-col gap-8 pt-4'>
            {/* Fork C: the scoring model is SHARED — co-searchers on this
                target score with the same rubric, so it is view-only per user
                (SEC-2, #366). It's shaped through the bounded #191 path
                (reference JDs + the learning log), not edited directly. The
                per-user knobs live in "Your view". */}
            <section
              aria-labelledby='shared-scoring-heading'
              className='flex flex-col gap-2'
            >
              <div className='flex items-center gap-2 flex-wrap'>
                <Heading variant='section' as='h2' id='shared-scoring-heading'>
                  Scoring model
                </Heading>
                <Badge variant='default' size='sm'>
                  Shared with co-searchers
                </Badge>
              </div>
              <Text variant='meta'>
                This model is shared by everyone pursuing this target.
                It&rsquo;s refined through reference JDs and the learning log —
                not edited directly.
              </Text>
              <ScoringProfileView target={target} />
            </section>

            {userTarget && (
              <section
                aria-labelledby='your-view-heading'
                className='flex flex-col gap-2'
              >
                <div className='flex items-center gap-2 flex-wrap'>
                  <Heading variant='section' as='h2' id='your-view-heading'>
                    Your view
                  </Heading>
                  <Badge variant='default' size='sm'>
                    Only you
                  </Badge>
                </div>
                <Text variant='meta'>
                  Axis weights re-rank the shared scores for your list only.
                </Text>
                <AxisWeightsEditor
                  targetId={id}
                  userTarget={userTarget}
                  onUpdated={setUserTarget}
                />
              </section>
            )}
          </div>
        ),
      },
      {
        id: 'preferences',
        label: 'Preferences',
        content: (
          <div className='flex flex-col gap-6 pt-4'>
            {userTarget ? (
              <>
                <TargetPreferencesEditor targetId={id} />
                <NotificationThresholdsEditor
                  targetId={id}
                  userTarget={userTarget}
                  onUpdated={setUserTarget}
                />
              </>
            ) : (
              <Text variant='body' className='text-text-secondary'>
                Preferences are per-user — activate this target to configure
                yours.
              </Text>
            )}
          </div>
        ),
      },
      {
        id: 'jds',
        label: 'Reference JDs',
        content: (
          <div className='pt-4'>
            {jdsLoaded ? (
              <ReferenceJDList
                targetId={id}
                referenceJDs={referenceJDs}
                onChanged={handleRefresh}
              />
            ) : (
              <Skeleton variant='rectangular' height={160} />
            )}
          </div>
        ),
      },
      {
        id: 'learning',
        label: 'Learning',
        content: (
          <div className='pt-4'>
            <LearningLogPanel targetId={id} onProfileChanged={fetchTarget} />
          </div>
        ),
      },
    ];
  }, [
    target,
    userTarget,
    id,
    jdsLoaded,
    referenceJDs,
    fetchTarget,
    handleRefresh,
  ]);

  if (loading) {
    return <TargetDetailSkeleton />;
  }

  if (!target) {
    return (
      <div className='flex flex-col items-center gap-4 py-20'>
        <Heading variant='hero' as='h1'>
          Target not found
        </Heading>
        <Link href='/targets' className='text-brand-500 hover:underline'>
          Back to targets
        </Link>
      </div>
    );
  }

  return (
    <div className='flex flex-col gap-6'>
      <Breadcrumbs
        items={[
          { label: 'Targets', href: '/targets' },
          { label: crumbLabel(target.label) },
        ]}
      />

      {/* Persistent header — visible on every tab. The label is a shared
          catalog field, so it's read-only per user (SEC-2, #366). */}
      <div className='flex items-center gap-3 flex-wrap'>
        <Heading variant='hero' as='h1'>
          {target.label}
        </Heading>

        {target.is_active && (
          <Badge variant='brand-solid' size='sm'>
            Active
          </Badge>
        )}
        {userTarget?.fit_score != null && (
          <Badge variant='default' size='sm'>
            Fit {userTarget.fit_score}
          </Badge>
        )}
      </div>

      <Tabs
        tabs={tabs}
        defaultTab={initialTabRef.current}
        variant='underline'
        onChange={handleTabChange}
      />
    </div>
  );
}
