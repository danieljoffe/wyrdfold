import type * as Sentry from '@sentry/nextjs';
import { publicEnv } from '@/lib/public.env';
import { scrubBreadcrumb, scrubEvent } from '@/lib/sentry.scrub';
import { isProduction } from '@/utils/helpers';

export const sentryEnabled = !!publicEnv.NEXT_PUBLIC_SENTRY_CONFIG_ID;

export const sharedSentryConfig: Parameters<typeof Sentry.init>[0] = {
  dsn: publicEnv.NEXT_PUBLIC_SENTRY_CONFIG_ID as string,
  environment: publicEnv.NEXT_PUBLIC_NODE_ENV,
  tracesSampleRate: isProduction() ? 0.1 : 1.0,
  sampleRate: 1.0,
  // The default, stated so nobody flips it: no IPs, cookies or bodies.
  // scrubEvent removes what the SDK attaches even so (cookies, the
  // Authorization header and request bodies all arrived without it).
  sendDefaultPii: false,
  beforeSend: scrubEvent,
  beforeSendTransaction: scrubEvent,
  beforeBreadcrumb: scrubBreadcrumb,
  // Sentry Logs stays OFF (#29 H-r2-5): nothing here uses Sentry.logger or
  // consoleLoggingIntegration, and the Logs stream bypasses scrubEvent, so it
  // has no scrubber — a stray console.log of resume text / prompts / tokens
  // would ship to Sentry unredacted. If logs are ever wanted, enable this
  // together with a beforeSendLog scrubber.
  enableLogs: false,
  ignoreErrors: ['NEXT_NOT_FOUND', 'NEXT_REDIRECT'],
  debug: false,
};
