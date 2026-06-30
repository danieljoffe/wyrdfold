'use client';

import { useCallback, useEffect, useState } from 'react';
import { Check, Sparkles, X } from 'lucide-react';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@danieljoffe/shared-ui/Card';
import { Badge, type BadgeVariant } from '@danieljoffe/shared-ui/Badge';
import { Text } from '@danieljoffe/shared-ui/Text';
import { Spinner } from '@danieljoffe/shared-ui/Spinner';
import Button from '@/components/Button';
import { extractApiError } from '@/lib/extractApiError';
import { useToast } from '@/state/Toast/ToastProvider';
import type {
  LearningRunResult,
  LearningStatus,
  ProfilePatchDiff,
  TargetLearningLogRow,
} from '../types';

interface LearningLogPanelProps {
  targetId: string;
  /** Called after a patch is applied — the shared scoring profile changed, so
   * the parent should refetch the target to reflect the new profile_version. */
  onProfileChanged: () => void;
}

const STATUS_BADGE: Record<
  LearningStatus,
  { variant: BadgeVariant; label: string }
> = {
  applied: { variant: 'success', label: 'Applied' },
  rejected: { variant: 'default', label: 'Rejected' },
  staged: { variant: 'info', label: 'Staged' },
};

/** Render a ProfilePatch diff as labeled keyword chips, skipping empty
 * groups. Each bucket maps to the scoring-profile change it represents. */
function DiffChips({ diff }: { diff: Partial<ProfilePatchDiff> }) {
  const all: { label: string; variant: BadgeVariant; items: string[] }[] = [
    {
      label: 'Boost skills',
      variant: 'success',
      items: Object.entries(diff.add_secondary ?? {}).map(
        ([kw, weight]) => `${kw} ×${weight}`
      ),
    },
    { label: 'Add filter', variant: 'error', items: diff.add_negative ?? [] },
    {
      label: 'Remove filter',
      variant: 'info',
      items: diff.remove_negative ?? [],
    },
    { label: 'Demote', variant: 'warning', items: diff.demote_keywords ?? [] },
  ];
  const groups = all.filter(g => g.items.length > 0);

  if (groups.length === 0) {
    return (
      <Text variant='meta' as='p' className='text-text-tertiary'>
        No profile changes.
      </Text>
    );
  }

  return (
    <div className='flex flex-col gap-1.5'>
      {groups.map(g => (
        <div key={g.label} className='flex flex-wrap items-center gap-1.5'>
          <Text
            variant='meta'
            as='span'
            className='shrink-0 text-text-secondary'
          >
            {g.label}
          </Text>
          {g.items.map(item => (
            <Badge key={item} variant={g.variant} size='sm'>
              {item}
            </Badge>
          ))}
        </div>
      ))}
    </div>
  );
}

/** Short provenance line: how many signals drove the patch and (if the
 * projection was computed) how many existing jobs it would move. */
function provenance(row: TargetLearningLogRow): string {
  const n = row.signals_consumed;
  const signals = `${n} signal${n === 1 ? '' : 's'}`;
  const p = row.projection;
  if (!p) return `From ${signals}`;
  return `From ${signals} · would move ${p.jobs_moved}/${p.jobs_considered} recent jobs`;
}

function StagedRow({
  row,
  acting,
  disabled,
  onApply,
  onReject,
}: {
  row: TargetLearningLogRow;
  acting: boolean;
  disabled: boolean;
  onApply: () => void;
  onReject: () => void;
}) {
  return (
    <div className='flex flex-col gap-3 rounded-lg border border-brand-500/40 bg-brand-500/5 p-3'>
      <div className='flex items-start justify-between gap-3'>
        <DiffChips diff={row.diff} />
        <Badge variant='info' size='sm'>
          {Math.round(row.confidence * 100)}% confident
        </Badge>
      </div>
      {row.rationale && (
        <Text variant='caption' as='p' className='text-text-secondary'>
          {row.rationale}
        </Text>
      )}
      <div className='flex items-center justify-between gap-3'>
        <Text variant='meta' as='span' className='text-text-tertiary'>
          {provenance(row)}
        </Text>
        <div className='flex shrink-0 items-center gap-2'>
          <Button
            name={`target-learn-reject-${row.id}`}
            variant='outline'
            size='sm'
            onClick={onReject}
            disabled={disabled}
          >
            <X className='size-4' aria-hidden />
            <span>Reject</span>
          </Button>
          <Button
            name={`target-learn-apply-${row.id}`}
            variant='primary'
            size='sm'
            onClick={onApply}
            disabled={disabled}
            aria-busy={acting}
          >
            {acting ? (
              <Spinner size='sm' aria-hidden />
            ) : (
              <Check className='size-4' aria-hidden />
            )}
            <span>Apply</span>
          </Button>
        </div>
      </div>
    </div>
  );
}

function HistoryRow({ row }: { row: TargetLearningLogRow }) {
  const badge = STATUS_BADGE[row.status];
  return (
    <div className='flex items-start justify-between gap-3 rounded-lg border border-border p-3'>
      <div className='flex min-w-0 flex-col gap-1.5'>
        <DiffChips diff={row.diff} />
        {row.rationale && (
          <Text
            variant='meta'
            as='p'
            className='line-clamp-2 text-text-tertiary'
          >
            {row.rationale}
          </Text>
        )}
      </div>
      <div className='flex shrink-0 flex-col items-end gap-1'>
        <Badge variant={badge.variant} size='sm'>
          {badge.label}
        </Badge>
        <Text variant='meta' as='span' className='text-text-tertiary'>
          {new Date(row.created_at).toLocaleDateString()}
        </Text>
      </div>
    </div>
  );
}

/**
 * Per-target feedback learning loop (#79). Lists the `target_learning_log`:
 * staged ProfilePatches the user can apply or reject, plus an audit history of
 * applied / rejected ones. "Check for updates" force-runs the LLM learner over
 * recent feedback so the user doesn't have to wait for the background trigger.
 */
export default function LearningLogPanel({
  targetId,
  onProfileChanged,
}: LearningLogPanelProps) {
  // null = still loading; [] = loaded-but-empty.
  const [rows, setRows] = useState<TargetLearningLogRow[] | null>(null);
  const [actingId, setActingId] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const { toast } = useToast();

  const fetchLog = useCallback(async () => {
    try {
      const res = await fetch(`/api/targets/${targetId}/learning-log?limit=50`);
      if (!res.ok) throw new Error('Failed to fetch learning log');
      setRows((await res.json()) as TargetLearningLogRow[]);
    } catch {
      // Non-fatal — render an empty panel rather than spin forever.
      setRows([]);
      toast({ variant: 'error', title: 'Failed to load learning history' });
    }
  }, [targetId, toast]);

  useEffect(() => {
    fetchLog();
  }, [fetchLog]);

  const act = useCallback(
    async (row: TargetLearningLogRow, action: 'apply' | 'reject') => {
      setActingId(row.id);
      try {
        // The apply/reject endpoints key on the staged log row's own id.
        const res = await fetch(
          `/api/targets/${targetId}/learn/${row.id}/${action}`,
          { method: 'POST' }
        );
        if (!res.ok) {
          throw new Error(
            await extractApiError(
              res,
              action === 'apply'
                ? 'Failed to apply update'
                : 'Failed to reject update'
            )
          );
        }
        const result = (await res.json()) as LearningRunResult;
        toast({
          variant: 'success',
          title:
            action === 'apply'
              ? 'Update applied — your scoring profile changed'
              : 'Update rejected',
        });
        await fetchLog();
        // Applying mutates the shared profile + re-scores; refresh the parent
        // so the profile editor / version reflect it.
        if (action === 'apply' && result.applied) onProfileChanged();
      } catch (err) {
        toast({
          variant: 'error',
          title: err instanceof Error ? err.message : 'Action failed',
        });
      } finally {
        setActingId(null);
      }
    },
    [targetId, toast, fetchLog, onProfileChanged]
  );

  const runLearner = useCallback(async () => {
    setRunning(true);
    try {
      const res = await fetch(`/api/targets/${targetId}/learn-llm`, {
        method: 'POST',
      });
      if (!res.ok) {
        throw new Error(await extractApiError(res, 'Failed to run learner'));
      }
      // A `null` body means there was nothing above threshold to learn from.
      const result = (await res.json()) as LearningRunResult | null;
      if (result === null) {
        toast({
          variant: 'info',
          title: 'No new patterns to learn from your recent feedback yet',
        });
        return;
      }
      toast({
        variant: 'success',
        title: result.applied
          ? 'Learned and applied a profile update'
          : 'A profile update is staged for your review below',
      });
      await fetchLog();
      if (result.applied) onProfileChanged();
    } catch (err) {
      toast({
        variant: 'error',
        title: err instanceof Error ? err.message : 'Failed to run learner',
      });
    } finally {
      setRunning(false);
    }
  }, [targetId, toast, fetchLog, onProfileChanged]);

  const staged = (rows ?? []).filter(r => r.status === 'staged');
  const history = (rows ?? []).filter(r => r.status !== 'staged');

  return (
    <Card>
      <CardHeader>
        <div className='flex items-start justify-between gap-3'>
          <div className='flex flex-col gap-0.5'>
            <CardTitle>Learning log</CardTitle>
            <Text variant='meta' as='span' className='text-text-secondary'>
              Scoring-profile updates wyrdfold proposes from your relevant /
              irrelevant feedback.
            </Text>
          </div>
          <Button
            name='target-learn-run'
            variant='outline'
            size='sm'
            onClick={runLearner}
            disabled={running}
            aria-busy={running}
          >
            {running ? (
              <Spinner size='sm' aria-hidden />
            ) : (
              <Sparkles className='size-4' aria-hidden />
            )}
            <span>{running ? 'Analyzing…' : 'Check for updates'}</span>
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        {rows === null ? (
          <Text variant='body' as='p' className='text-text-secondary'>
            Loading…
          </Text>
        ) : rows.length === 0 ? (
          <Text variant='body' as='p' className='text-text-secondary'>
            No learning activity yet. As you mark jobs relevant or irrelevant,
            wyrdfold proposes scoring-profile updates here for your review.
          </Text>
        ) : (
          <div className='flex flex-col gap-5'>
            {staged.length > 0 && (
              <div className='flex flex-col gap-3'>
                <h3 className='text-sm font-semibold text-text-primary'>
                  Staged for review ({staged.length})
                </h3>
                {staged.map(row => (
                  <StagedRow
                    key={row.id}
                    row={row}
                    acting={actingId === row.id}
                    disabled={actingId !== null}
                    onApply={() => act(row, 'apply')}
                    onReject={() => act(row, 'reject')}
                  />
                ))}
              </div>
            )}
            {history.length > 0 && (
              <div className='flex flex-col gap-2'>
                <h3 className='text-sm font-semibold text-text-primary'>
                  History
                </h3>
                {history.map(row => (
                  <HistoryRow key={row.id} row={row} />
                ))}
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
