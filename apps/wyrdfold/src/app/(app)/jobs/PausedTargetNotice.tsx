'use client';

import { useCallback, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Card, CardContent } from '@danieljoffe/shared-ui/Card';
import { Heading } from '@danieljoffe/shared-ui/Heading';
import { Text } from '@danieljoffe/shared-ui/Text';
import Button from '@/components/kit/Button';
import { extractApiError } from '@/lib/extractApiError';
import { useToast } from '@/state/Toast/ToastProvider';

interface PausedTargetNoticeProps {
  targetId: string;
  label: string;
}

/**
 * "You asked for a paused target's jobs" explainer.
 *
 * `/jobs` scopes to ACTIVE memberships only (Group D schema decision,
 * docs/decisions.md) — a paused target genuinely has no rows here, so its tab
 * is omitted. The page used to handle a deep link to one by falling through to
 * the unknown-target guard and silently `redirect('/jobs')`, which drops the
 * user on the all-jobs list showing whichever OTHER target happens to be
 * active. The request was well-formed and the answer knowable; discarding it
 * without a word is the part that was wrong, not the scoping.
 *
 * So: keep the scoping, name the target the user actually asked for, and offer
 * the one action that makes its jobs exist.
 */
export default function PausedTargetNotice({
  targetId,
  label,
}: PausedTargetNoticeProps) {
  const { toast } = useToast();
  const router = useRouter();
  const [activating, setActivating] = useState(false);

  const handleActivate = useCallback(async () => {
    setActivating(true);
    try {
      const res = await fetch(`/api/targets/${targetId}/activate`, {
        method: 'POST',
      });
      if (!res.ok) {
        throw new Error(await extractApiError(res, 'Activate failed'));
      }
      toast({
        variant: 'success',
        title: `Resumed “${label}”`,
        description:
          'Matching restarts on the next poll — its jobs will appear here shortly.',
      });
      router.refresh();
    } catch (err) {
      toast({
        variant: 'error',
        title: err instanceof Error ? err.message : 'Activate failed',
      });
    } finally {
      setActivating(false);
    }
  }, [targetId, label, toast, router]);

  return (
    <Card padding='none'>
      <CardContent className='flex flex-col items-start gap-2 p-4'>
        <Heading variant='cardTitle'>&ldquo;{label}&rdquo; is paused</Heading>
        <Text variant='body' as='p' className='text-text-secondary'>
          Paused targets aren&rsquo;t matched against new postings, so this one
          has no jobs to show yet. The list below is all your jobs instead.
        </Text>
        <div className='mt-1 flex flex-wrap items-center gap-2'>
          <Button
            name='paused-target-activate'
            variant='primary'
            size='sm'
            onClick={handleActivate}
            disabled={activating}
            aria-busy={activating}
          >
            {activating ? 'Resuming…' : `Resume “${label}”`}
          </Button>
          <Button
            name='paused-target-manage'
            variant='outline'
            size='sm'
            onClick={() => router.push('/targets')}
          >
            Manage targets
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
