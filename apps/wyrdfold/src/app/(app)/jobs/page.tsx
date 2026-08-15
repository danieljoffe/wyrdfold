import type { Metadata } from 'next';
import { redirect } from 'next/navigation';
import { fetchJsonFromWyrdfoldAPI } from '@/lib/api/proxy';
import type { UserTargetWithSummary } from '../targets/types';
import JobsList, { type TargetTab } from './JobsList';
import PausedTargetNotice from './PausedTargetNotice';
import { resolveRequestedTarget, toActiveTargetTabs } from './targetTabs';
import { JOB_STATUSES, type JobStatus } from './types';

export const metadata: Metadata = {
  title: 'Jobs',
};

const STATUS_SET = new Set<string>(JOB_STATUSES);

export default async function FittedJobsPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const targetId =
    typeof params.target === 'string' ? params.target : undefined;
  const rawStatus = typeof params.status === 'string' ? params.status : '';
  const initialStatus: JobStatus | '' = STATUS_SET.has(rawStatus)
    ? (rawStatus as JobStatus)
    : '';
  const rawMinScore =
    typeof params.minScore === 'string' ? params.minScore : '';
  const parsedMinScore = Number.parseInt(rawMinScore, 10);
  const initialMinScore =
    Number.isFinite(parsedMinScore) &&
    parsedMinScore >= 0 &&
    parsedMinScore <= 100
      ? String(parsedMinScore)
      : '';

  const targetsRes = await fetchJsonFromWyrdfoldAPI<{
    targets: UserTargetWithSummary[];
  }>('/targets/mine');
  // Paused (deactivated) targets are omitted entirely — see toActiveTargetTabs.
  // A deep link to a paused target falls through to the redirect below.
  const initialTargets: TargetTab[] = toActiveTargetTabs(
    targetsRes?.targets ?? []
  );

  // An unrenderable `?target=` used to mean one thing (silent redirect); it
  // actually means two. See resolveRequestedTarget for the split — a paused
  // membership now gets named and offered a resume instead of being dropped
  // on whichever other target happens to be active.
  const resolution = resolveRequestedTarget(
    targetId,
    targetsRes?.targets ?? []
  );
  if (resolution.kind === 'redirect') {
    // Server-side redirect avoids a render → effect → client redirect waterfall.
    redirect('/jobs');
  }
  const pausedTarget =
    resolution.kind === 'paused' ? resolution.target : undefined;

  return (
    <>
      {pausedTarget && (
        <div className='mb-4'>
          <PausedTargetNotice
            targetId={pausedTarget.id}
            label={pausedTarget.label}
          />
        </div>
      )}
      <JobsList
        targetId={pausedTarget ? undefined : targetId}
        initialStatus={initialStatus}
        initialMinScore={initialMinScore}
        initialTargets={initialTargets}
      />
    </>
  );
}
