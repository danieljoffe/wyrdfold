/**
 * What may leave the app in a Sentry event. The fixtures mirror the shape of
 * a real server error and transaction captured from a production build
 * (`next build` + `next start`) against a stand-in ingest, with the same
 * planted values: before scrubbing, every one of them was in the payload.
 */
import type { ErrorEvent, Event } from '@sentry/nextjs';
import { scrubBreadcrumb, scrubEvent, stripQueries } from '../sentry.scrub';

const PLANTED = [
  'SESSION-SECRET',
  'JWT-SECRET',
  '203.0.113.7',
  'PKCE-CODE',
  'TOKEN-HASH',
  'RESUME-BODY',
  'jane@example.com',
  'CONSOLE-LINE',
  'SEARCH-TERM',
  'REFERRER-TOKEN',
];

const FRAME_FILENAME = 'app:///_next/static/chunks/main.js?dpl=dpl_123';

function serverError(): ErrorEvent {
  return {
    type: undefined,
    message: 'callback failed at /auth/callback?code=PKCE-CODE',
    request: {
      method: 'POST',
      url: 'http://localhost:3210/auth/callback?code=PKCE-CODE&next=/jobs',
      query_string: 'code=PKCE-CODE',
      cookies: { 'sb-abc-auth-token': 'SESSION-SECRET', theme: 'dark' },
      headers: {
        cookie: 'sb-abc-auth-token=SESSION-SECRET; theme=dark',
        authorization: 'Bearer JWT-SECRET',
        'x-forwarded-for': '203.0.113.7',
        'User-Agent': 'probe-agent/1.0',
        referer: 'http://localhost:3210/login?token_hash=TOKEN-HASH',
      },
      data: '{"resume":"RESUME-BODY jane@example.com"}',
      env: { REMOTE_ADDR: '203.0.113.7' },
    },
    contexts: {
      nextjs: { request_path: '/auth/callback?code=PKCE-CODE' },
      trace: {
        trace_id: 'a'.repeat(32),
        span_id: 'b'.repeat(16),
        data: {
          'http.method': 'POST',
          'http.target': '/auth/callback?code=PKCE-CODE',
          'http.request.header.cookie.sb-abc-auth-token': 'SESSION-SECRET',
          'url.query': '?code=PKCE-CODE',
        },
      },
    },
    breadcrumbs: [
      {
        category: 'console',
        message: 'CONSOLE-LINE',
        data: { arguments: ['CONSOLE-LINE'] },
      },
      {
        category: 'navigation',
        data: { from: '/search?q=SEARCH-TERM', to: '/jobs#top' },
      },
      {
        category: 'fetch',
        data: {
          method: 'GET',
          url: 'http://localhost:3210/api/jobs?q=SEARCH-TERM',
          status_code: 200,
        },
      },
    ],
    tags: {
      'page.url': '/jobs',
      'page.referrer': 'https://mail.example.com/inbox?t=REFERRER-TOKEN',
    },
    exception: {
      values: [
        {
          type: 'Error',
          value: 'fetch failed for /api/x?token_hash=TOKEN-HASH',
          stacktrace: {
            frames: [
              { filename: FRAME_FILENAME, function: 'probe', lineno: 1 },
            ],
          },
        },
      ],
    },
    user: { id: 'u-1', email: 'jane@example.com', ip_address: '203.0.113.7' },
  };
}

function serverTransaction(): Event {
  const { request, contexts } = serverError();
  return {
    type: 'transaction',
    transaction: 'GET /auth/callback',
    ...(request && { request }),
    ...(contexts && { contexts }),
    spans: [
      {
        span_id: 'c'.repeat(16),
        trace_id: 'a'.repeat(32),
        start_timestamp: 1,
        description: 'GET http://api.local/jobs?search=SEARCH-TERM',
        data: {
          'url.full': 'http://api.local/jobs?search=SEARCH-TERM',
          'http.request.header.authorization': 'Bearer JWT-SECRET',
          'http.response.status_code': 200,
        },
      },
    ],
  };
}

describe('stripQueries', () => {
  it.each([
    ['http://h.test/a?b=1#c', 'http://h.test/a'],
    ['http://h.test/a#frag', 'http://h.test/a'],
    ['/login?next=/jobs', '/login'],
    ['GET /api/x?q=1 failed', 'GET /api/x failed'],
    ['{"url":"/api/x?y=1"}', '{"url":"/api/x"}'],
    ['/a/b', '/a/b'],
    [
      'Unexpected token ? in JSON at position 0',
      'Unexpected token ? in JSON at position 0',
    ],
    ['a/b?c', 'a/b?c'],
    // Frames themselves are skipped by the walk; see 'keeps stack frames verbatim'.
    [FRAME_FILENAME, 'app:///_next/static/chunks/main.js'],
    // Query runs with no URL or path in front of them.
    ['?token_hash=TOKEN-HASH&type=bogus', ''],
    ['search: ?token_hash=TOKEN-HASH', 'search: '],
    ['#access_token=JWT-SECRET&refresh_token=SESSION-SECRET', ''],
    ['wyrdfold.com/auth/callback?code=PKCE-CODE', 'wyrdfold.com/auth/callback'],
    ['Issue #123 is fixed', 'Issue #123 is fixed'],
    ['color: #fff', 'color: #fff'],
  ])('%s -> %s', (input, expected) => {
    expect(stripQueries(input)).toBe(expected);
  });
});

describe('scrubEvent', () => {
  it('leaves none of the planted values in a server error', () => {
    const json = JSON.stringify(scrubEvent(serverError()));
    for (const value of PLANTED) expect(json).not.toContain(value);
  });

  it('keeps what debugging needs', () => {
    const event = scrubEvent(serverError());

    expect(event.request).toEqual({
      method: 'POST',
      url: 'http://localhost:3210/auth/callback',
      headers: {
        'User-Agent': 'probe-agent/1.0',
        referer: 'http://localhost:3210/login',
      },
    });
    expect(event.contexts?.nextjs).toEqual({ request_path: '/auth/callback' });
    expect(event.contexts?.trace?.data).toEqual({
      'http.method': 'POST',
      'http.target': '/auth/callback',
    });
    expect(event.breadcrumbs?.map(b => b.category)).toEqual([
      'navigation',
      'fetch',
    ]);
    expect(event.breadcrumbs?.[0]?.data).toEqual({
      from: '/search',
      to: '/jobs',
    });
    expect(event.exception?.values?.[0]?.value).toBe('fetch failed for /api/x');
    expect(event.tags?.['page.referrer']).toBe(
      'https://mail.example.com/inbox'
    );
    expect(event.user).toEqual({ id: 'u-1' });
  });

  it('keeps stack frames verbatim', () => {
    const frames =
      scrubEvent(serverError()).exception?.values?.[0]?.stacktrace?.frames;
    expect(frames?.[0]?.filename).toBe(FRAME_FILENAME);
  });

  it('removes a bare query string captured as extra data', () => {
    // auth/callback used to send `url.search` as-is; with an unrecognised
    // `type` that is a live token_hash with nothing in front of it.
    const event = serverError();
    event.extra = { search: '?token_hash=TOKEN-HASH&type=bogus' };

    const json = JSON.stringify(scrubEvent(event));

    expect(json).not.toContain('TOKEN-HASH');
    expect(scrubEvent(event).extra).toEqual({ search: '' });
  });

  it('drops a user that has no id', () => {
    const event = serverError();
    event.user = { email: 'jane@example.com' };
    expect(scrubEvent(event).user).toBeUndefined();
  });

  it('does not mutate the event it was given', () => {
    const event = serverError();
    const before = JSON.stringify(event);
    scrubEvent(event);
    expect(JSON.stringify(event)).toBe(before);
  });

  it('copes with circular references and passes SDK metadata through', () => {
    // A sampled server transaction carries scope objects with circular
    // references in sdkProcessingMetadata; walking them overflowed the stack
    // in a production build, and the SDK lost the transaction.
    const scope: Record<string, unknown> = { name: 'scope' };
    scope['self'] = scope;
    const metadata = { scopeWithCycle: scope };
    const extra: Record<string, unknown> = { url: '/a?b=1' };
    extra['parent'] = extra;
    const event = serverTransaction();
    event.sdkProcessingMetadata = metadata;
    event.extra = extra;

    const scrubbed = scrubEvent(event);

    expect(scrubbed.sdkProcessingMetadata).toBe(metadata);
    expect(scrubbed.extra?.['url']).toBe('/a');
    expect(scrubbed.extra?.['parent']).toBe(scrubbed.extra);
  });

  it('scrubs transactions and their spans', () => {
    const event = scrubEvent(serverTransaction());
    const json = JSON.stringify(event);
    for (const value of PLANTED) expect(json).not.toContain(value);
    expect(event.spans?.[0]?.description).toBe('GET http://api.local/jobs');
    expect(event.spans?.[0]?.data).toEqual({
      'url.full': 'http://api.local/jobs',
      'http.response.status_code': 200,
    });
  });
});

describe('scrubBreadcrumb', () => {
  it('drops console breadcrumbs', () => {
    expect(
      scrubBreadcrumb({ category: 'console', message: 'CONSOLE-LINE' })
    ).toBeNull();
  });

  it('strips query strings from the rest', () => {
    expect(
      scrubBreadcrumb({
        category: 'navigation',
        data: { from: '/search?q=SEARCH-TERM', to: '/jobs' },
      })
    ).toEqual({
      category: 'navigation',
      data: { from: '/search', to: '/jobs' },
    });
  });
});

// The wiring: every runtime's init must route through the scrubber, and the
// browser must not bring Session Replay back (its URLs can't be scrubbed).
describe('Sentry init wiring', () => {
  const sentry = {
    init: jest.fn(),
    addIntegration: jest.fn(),
    browserTracingIntegration: jest.fn(() => ({ name: 'BrowserTracing' })),
    replayIntegration: jest.fn(),
    captureRouterTransitionStart: jest.fn(),
  };

  async function load(path: string) {
    jest.resetModules();
    jest.doMock('@sentry/nextjs', () => sentry);
    const previous = process.env['NEXT_PUBLIC_SENTRY_CONFIG_ID'];
    process.env['NEXT_PUBLIC_SENTRY_CONFIG_ID'] =
      'https://key@o1.ingest.us.sentry.io/1';
    try {
      await import(path);
      return await import('../sentry.scrub');
    } finally {
      if (previous === undefined)
        delete process.env['NEXT_PUBLIC_SENTRY_CONFIG_ID'];
      else process.env['NEXT_PUBLIC_SENTRY_CONFIG_ID'] = previous;
    }
  }

  beforeEach(() => jest.clearAllMocks());

  it.each([
    ['browser', '../../instrumentation-client'],
    ['nodejs', '../../sentry.server.config'],
    ['edge', '../../sentry.edge.config'],
  ])(
    '%s init routes events, transactions and breadcrumbs through the scrubber',
    async (_runtime, path) => {
      const scrub = await load(path);

      expect(sentry.init).toHaveBeenCalledTimes(1);
      const options = sentry.init.mock.calls[0][0];
      expect(options.sendDefaultPii).toBe(false);
      expect(options.beforeSendTransaction).toBe(scrub.scrubEvent);
      expect(options.beforeBreadcrumb).toBe(scrub.scrubBreadcrumb);
      const sent = JSON.stringify(options.beforeSend(serverError(), {}));
      for (const value of PLANTED) expect(sent).not.toContain(value);
    }
  );

  it('browser init never enables Session Replay', async () => {
    jest.useFakeTimers();
    try {
      await load('../../instrumentation-client');
      jest.runAllTimers();
    } finally {
      jest.useRealTimers();
    }
    const options = sentry.init.mock.calls[0][0];
    expect(options.replaysSessionSampleRate).toBeUndefined();
    expect(options.replaysOnErrorSampleRate).toBeUndefined();
    expect(sentry.replayIntegration).not.toHaveBeenCalled();
    expect(sentry.addIntegration).toHaveBeenCalledTimes(1);
  });
});
