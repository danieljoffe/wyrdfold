import * as Sentry from '@sentry/nextjs';
import { sentryEnabled } from '@/lib/sentry.config';
import { scrubBreadcrumb, scrubEvent } from '@/lib/sentry.scrub';
import { publicEnv } from '@/lib/public.env';
import { isProduction } from '@/utils/helpers';

if (sentryEnabled) {
  Sentry.init({
    dsn: publicEnv.NEXT_PUBLIC_SENTRY_CONFIG_ID as string,
    environment: publicEnv.NEXT_PUBLIC_NODE_ENV,
    tracesSampleRate: isProduction() ? 0.1 : 1.0,
    sampleRate: 1.0,
    // The default, stated so nobody flips it: no IPs, cookies or bodies.
    // scrubEvent below removes what the SDK attaches even so.
    sendDefaultPii: false,
    // OFF (#29 H-r2-5): no Sentry.logger / consoleLoggingIntegration usage,
    // and the Logs stream bypasses scrubEvent, so it would ship unscrubbed —
    // see the rationale in lib/sentry.config.ts.
    enableLogs: false,

    // Heavy integrations are deferred below to avoid blocking LCP.
    // Session Replay stays off: its DOM recording stores each page's full URL,
    // query string included, and unlike errors and transactions those
    // recording events can't be scrubbed.
    integrations: [],

    ignoreErrors: [
      /extensions\//i,
      /^chrome-extension:\/\//,
      /^moz-extension:\/\//,
      'Network request failed',
      'Failed to fetch',
      'Load failed',
      'ResizeObserver loop limit exceeded',
      'ResizeObserver loop completed with undelivered notifications',
    ],

    beforeSend(event) {
      if (typeof window !== 'undefined') {
        event.tags = {
          ...event.tags,
          'page.url': window.location.pathname,
          'page.referrer': document.referrer || 'direct',
        };
      }
      return scrubEvent(event);
    },
    beforeSendTransaction: scrubEvent,
    beforeBreadcrumb: scrubBreadcrumb,

    debug: false,
  });

  if (typeof window !== 'undefined') {
    const loadDeferredIntegrations = () => {
      Sentry.addIntegration(Sentry.browserTracingIntegration());
    };

    if ('requestIdleCallback' in window) {
      window.requestIdleCallback(loadDeferredIntegrations);
    } else {
      setTimeout(loadDeferredIntegrations, 0);
    }
  }
}

export const onRouterTransitionStart = sentryEnabled
  ? Sentry.captureRouterTransitionStart
  : undefined;
