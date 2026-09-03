'use client';

import * as Sentry from '@sentry/nextjs';
import { useEffect } from 'react';
import StatusCard from '@/components/StatusCard';
import Button from '@/components/kit/Button';
import LinkButton from '@/components/kit/LinkButton';

export default function AppError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    Sentry.withScope(scope => {
      scope.setTag('route', '/(app)');
      if (error.digest) {
        scope.setExtra('digest', error.digest);
      }
      Sentry.captureException(error);
    });
  }, [error]);

  return (
    <div className='flex min-h-full items-center justify-center py-12'>
      <StatusCard
        title='Something went wrong'
        body='This page failed to load. The error has been reported. Try again, or head back to your dashboard.'
        actions={
          <>
            <Button
              name='wyrdfold-error-retry'
              variant='primary'
              size='sm'
              onClick={() => reset()}
            >
              Try again
            </Button>
            <LinkButton
              name='wyrdfold-error-home'
              variant='outline'
              size='sm'
              href='/dashboard'
            >
              Back to dashboard
            </LinkButton>
          </>
        }
      />
    </div>
  );
}
