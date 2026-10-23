import * as Sentry from '@sentry/nextjs';
import { sentryEnabled, sharedSentryConfig } from '@/lib/sentry.config';
import { scrubEvent } from '@/lib/sentry.scrub';

if (sentryEnabled) {
  Sentry.init({
    ...sharedSentryConfig,
    beforeSend(event) {
      event.tags = { ...event.tags, runtime: 'nodejs' };
      return scrubEvent(event);
    },
  });
}
