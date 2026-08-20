'use client';

import { useCallback, useState } from 'react';
import { useRouter } from 'next/navigation';
import { CheckCircle, ArrowRight, Target, Loader2 } from 'lucide-react';
import { Card } from '@danieljoffe/shared-ui/Card';
import { Text } from '@danieljoffe/shared-ui/Text';
import { Heading } from '@danieljoffe/shared-ui/Heading';
import { Alert } from '@danieljoffe/shared-ui/Alert';
import Button from '@/components/kit/Button';
import { completeOnboarding } from './completeOnboarding';

export default function CompletionScreen({
  targetsCreated,
}: {
  /** Targets created (or linked) during this wizard run. Drives the copy:
   *  a zero-target finish must not claim "you're all set" — matching never
   *  starts without an active target, so the screen says so and points the
   *  CTA at adding one (onboarding-sweep-2026-08-14 P2/A2). */
  targetsCreated: number;
}) {
  const router = useRouter();
  const [submitting, setSubmitting] = useState(false);
  const [failed, setFailed] = useState(false);
  const hasTargets = targetsCreated > 0;

  // Mark onboarding complete server-side, then navigate. The completion
  // call is idempotent on the API, so a double-click or retry from a
  // network hiccup won't overwrite the original timestamp.
  //
  // We only navigate once the write is CONFIRMED (HTTP 2xx).
  // ``completeOnboarding`` checks ``res.ok`` — previously a swallowed
  // non-2xx navigated away with ``onboarding_completed_at`` still NULL,
  // and the dashboard gate bounced the user right back here. On a
  // confirmed failure we surface a retry instead of looping.
  const handleContinue = useCallback(async () => {
    setSubmitting(true);
    setFailed(false);
    const ok = await completeOnboarding();
    if (!ok) {
      setSubmitting(false);
      setFailed(true);
      return;
    }
    router.push('/targets');
  }, [router]);

  return (
    <div className='flex flex-col items-center gap-6'>
      <Card padding='lg' className='w-full text-center'>
        <div className='flex flex-col items-center gap-4 py-4'>
          {hasTargets ? (
            <div className='rounded-full bg-success/10 p-4'>
              <CheckCircle className='size-12 text-success' aria-hidden />
            </div>
          ) : (
            <div className='rounded-full bg-surface-elevated p-4'>
              <Target className='size-12 text-text-tertiary' aria-hidden />
            </div>
          )}
          <div>
            <Heading variant='cardTitle' as='h2'>
              {hasTargets ? "You're all set!" : 'Setup saved'}
            </Heading>
            {hasTargets ? (
              <>
                <Text variant='body' className='mt-2 text-text-secondary'>
                  Your account is ready. Head to your targets to start tracking
                  jobs and generating tailored resumes.
                </Text>
                <Text variant='caption' className='mt-3 text-text-tertiary'>
                  WyrdFold sharpens as you mark which jobs you&apos;d actually
                  apply to. A few rounds in, the matches start finding you.
                </Text>
              </>
            ) : (
              <Text variant='body' className='mt-2 text-text-secondary'>
                You haven&apos;t added any job targets yet — matching only
                starts once at least one target is active. Add one whenever
                you&apos;re ready.
              </Text>
            )}
          </div>
        </div>
      </Card>

      {failed && (
        <Alert variant='error' className='w-full'>
          We couldn&apos;t finish setting up your account. Check your connection
          and try again.
        </Alert>
      )}

      <Button
        name='onboarding-go-to-targets'
        variant='primary'
        size='lg'
        className='w-full justify-center'
        onClick={handleContinue}
        disabled={submitting}
      >
        {submitting ? (
          <>
            <Loader2 className='size-4 animate-spin' aria-hidden />
            <span>Finishing up…</span>
          </>
        ) : (
          <>
            <span>{hasTargets ? 'Go to Targets' : 'Add a target'}</span>
            <ArrowRight className='size-4' aria-hidden />
          </>
        )}
      </Button>
    </div>
  );
}
