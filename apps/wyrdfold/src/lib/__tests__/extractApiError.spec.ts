import { extractApiError } from '../extractApiError';

// jsdom has no fetch Response global; extractApiError only touches
// `.status` and `.clone().json()`, so a minimal fake suffices.
function resWithDetail(status: number, detail: unknown): Response {
  const fake = {
    status,
    clone() {
      return { json: async () => ({ detail }) };
    },
  };
  return fake as unknown as Response;
}

function res429(detail: unknown): Response {
  return resWithDetail(429, detail);
}

describe('extractApiError — cost-cap shapes', () => {
  it('formats the monthly allowance breach with the rolling-window hint', async () => {
    const msg = await extractApiError(
      res429({
        code: 'llm_budget_exceeded',
        scope: 'monthly',
        limit_usd: 5,
        spent_usd: 5.12,
      }),
      'Request failed'
    );
    expect(msg).toBe(
      'Monthly LLM allowance reached ($5.12 of $5.00) — frees up as usage rolls out of the 30-day window.'
    );
  });

  it('keeps the hourly wording unchanged', async () => {
    const msg = await extractApiError(
      res429({
        code: 'llm_budget_exceeded',
        scope: 'hourly',
        limit_usd: 1,
        spent_usd: 1.01,
      }),
      'Request failed'
    );
    expect(msg).toBe(
      'LLM hourly budget reached ($1.01 of $1.00) — try again in an hour.'
    );
  });

  it('formats the analysis daily-count limit with the cached-revisit hint', async () => {
    const msg = await extractApiError(
      res429({ code: 'analysis_daily_limit', limit: 20, used: 20 }),
      'Analysis failed'
    );
    expect(msg).toBe(
      'Daily deep-analysis limit reached (20/day) — more tomorrow. Already-analyzed jobs stay free to revisit.'
    );
  });

  it('falls back with status code on unknown structured detail', async () => {
    const msg = await extractApiError(
      res429({ code: 'something_else' }),
      'Request failed'
    );
    expect(msg).toBe('Request failed (429)');
  });
});

describe('extractApiError — pydantic validation arrays gated by status', () => {
  const pydanticDetail = [
    {
      type: 'value_error',
      loc: ['body', 'phone'],
      msg: 'Value error, Phone must be E.164',
    },
  ];

  it('surfaces the first validation msg on a 422 (client error)', async () => {
    const msg = await extractApiError(
      resWithDetail(422, pydanticDetail),
      'Update failed'
    );
    // ``Value error,`` prefix is stripped.
    expect(msg).toBe('Phone must be E.164');
  });

  it('returns the generic fallback on a 500 even with a pydantic array (server bug)', async () => {
    const msg = await extractApiError(
      resWithDetail(
        500,
        // The analysis 500 shape: server failed to validate its OWN payload.
        [
          {
            type: 'missing',
            loc: ['response', 'scorecard'],
            msg: 'Field required',
          },
        ]
      ),
      'Analysis failed'
    );
    // Must NOT leak the raw validation msg ("Field required") to the user.
    expect(msg).toBe('Analysis failed (500)');
  });

  it('surfaces the no_profile (404) message and never leaks the internal path (#105)', async () => {
    const msg = await extractApiError(
      resWithDetail(404, {
        code: 'no_profile',
        message:
          'Set up your experience profile to generate a job-fit analysis.',
      }),
      'Analysis failed'
    );
    expect(msg).toBe(
      'Set up your experience profile to generate a job-fit analysis.'
    );
    expect(msg).not.toMatch(/experience\/derive/i);
    expect(msg).not.toMatch(/POST/);
  });
});

describe('extractApiError — ACTIVE_LIMIT (409)', () => {
  // The API composes this payload with an `error` key while every other
  // structured detail uses `code`, so none of the branches above matched and
  // the server's ready-made sentence was dropped one layer from the screen —
  // the user saw a bare "Activate failed (409)" and had no idea a cap existed.
  it('surfaces the server message verbatim', async () => {
    const msg = await extractApiError(
      resWithDetail(409, {
        error: 'ACTIVE_LIMIT',
        limit: 1,
        active_count: 1,
        message:
          'You already have 1 active target (limit 1) — deactivate one first.',
      }),
      'Activate failed'
    );
    expect(msg).toBe(
      'You already have 1 active target (limit 1) — deactivate one first.'
    );
    // The regression guard: never the bare status fallback.
    expect(msg).not.toMatch(/\(409\)/);
  });

  it('composes a true sentence when the server message is missing', async () => {
    const msg = await extractApiError(
      resWithDetail(409, { error: 'ACTIVE_LIMIT', limit: 3, active_count: 3 }),
      'Activate failed'
    );
    expect(msg).toBe('You can have 3 active targets — deactivate one first.');
  });

  it('singularizes the fallback for a cap of 1', async () => {
    const msg = await extractApiError(
      resWithDetail(409, { error: 'ACTIVE_LIMIT', limit: 1, active_count: 1 }),
      'Activate failed'
    );
    expect(msg).toBe('You can have 1 active target — deactivate one first.');
  });

  it('falls back to generic copy when even the limit is absent', async () => {
    const msg = await extractApiError(
      resWithDetail(409, { error: 'ACTIVE_LIMIT' }),
      'Activate failed'
    );
    expect(msg).toBe(
      'You are at your active-target limit — deactivate one first.'
    );
  });

  it('ignores a blank server message rather than showing an empty toast', async () => {
    const msg = await extractApiError(
      resWithDetail(409, { error: 'ACTIVE_LIMIT', limit: 2, message: '   ' }),
      'Activate failed'
    );
    expect(msg).toBe('You can have 2 active targets — deactivate one first.');
  });

  it('does NOT hijack other structured errors that happen to carry `error`', async () => {
    const msg = await extractApiError(
      resWithDetail(409, { error: 'SOMETHING_ELSE', message: 'nope' }),
      'Activate failed'
    );
    expect(msg).toBe('Activate failed (409)');
  });
});

// The BFF proxy routes (e.g. /api/public/search) normalize failures to a
// top-level ``{ error: "..." }`` — no ``detail`` key at all (#833).
function resWithBody(status: number, body: unknown): Response {
  const fake = {
    status,
    clone() {
      return { json: async () => body };
    },
  };
  return fake as unknown as Response;
}

describe('extractApiError — top-level BFF `error` shape (#833)', () => {
  it('surfaces a top-level error string instead of the bare status', async () => {
    const msg = await extractApiError(
      resWithBody(422, { error: 'Search needs a keyword or a filter.' }),
      'Search failed'
    );
    expect(msg).toBe('Search needs a keyword or a filter.');
  });

  it('lets a FastAPI detail win when both keys are present', async () => {
    const msg = await extractApiError(
      resWithBody(422, { detail: 'From the API.', error: 'From the BFF.' }),
      'Search failed'
    );
    expect(msg).toBe('From the API.');
  });

  it('still falls back on a non-string or blank error value', async () => {
    expect(
      await extractApiError(
        resWithBody(429, { error: { code: 'RATE_LIMITED' } }),
        'Search failed'
      )
    ).toBe('Search failed (429)');
    expect(
      await extractApiError(resWithBody(429, { error: '   ' }), 'Search failed')
    ).toBe('Search failed (429)');
  });
});
