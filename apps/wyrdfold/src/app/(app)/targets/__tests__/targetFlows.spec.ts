import {
  activateTargetInBackground,
  addSearchSuggestionTarget,
  addSuggestionTarget,
  createBareTarget,
  createOrLinkTarget,
  linkExistingTarget,
  linkTarget,
  toListEntry,
} from '../targetFlows';
import type {
  CreateOrLinkResult,
  JobTarget,
  MatchedSuggestion,
  UserTarget,
} from '../types';

const TARGET = {
  id: 't-1',
  label: 'Staff Engineer',
  description: null,
  is_active: true,
  activation_status: 'ready',
  search_keywords: ['staff engineer'],
  scoring_profile: { categories: [], domains: [], negatives: [] },
  created_at: '2026-07-01T00:00:00Z',
  updated_at: '2026-07-01T00:00:00Z',
} as unknown as JobTarget;

const USER_TARGET = {
  id: 'ut-1',
  user_id: 'u-1',
  target_id: 't-1',
  is_active: true,
  fit_score: 82,
} as unknown as UserTarget;

const CREATE_RESULT = {
  target: TARGET,
  user_target: USER_TARGET,
  was_matched: false,
} as unknown as CreateOrLinkResult;

function okResponse(payload: unknown): Response {
  const res = {
    ok: true,
    status: 201,
    json: async () => payload,
  } as unknown as Response;
  return res;
}

function errorResponse(status: number, detail?: unknown): Response {
  const body = detail === undefined ? {} : { detail };
  const res: Record<string, unknown> = {
    ok: false,
    status,
    json: async () => body,
  };
  res.clone = () => res;
  return res as unknown as Response;
}

const originalFetch = global.fetch;
let fetchMock: jest.Mock;

beforeEach(() => {
  fetchMock = jest.fn();
  global.fetch = fetchMock as unknown as typeof fetch;
});

afterEach(() => {
  global.fetch = originalFetch;
});

describe('createOrLinkTarget', () => {
  it('POSTs the JSON body to the given endpoint and returns the result', async () => {
    fetchMock.mockResolvedValue(okResponse(CREATE_RESULT));

    const result = await createOrLinkTarget('/api/targets/from-manual', {
      label: 'Staff Engineer',
      description: 'desc',
    });

    expect(fetchMock).toHaveBeenCalledWith('/api/targets/from-manual', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label: 'Staff Engineer', description: 'desc' }),
    });
    expect(result.target.id).toBe('t-1');
  });

  it('surfaces the server detail on failure', async () => {
    fetchMock.mockResolvedValue(errorResponse(422, 'No experience profile'));

    await expect(
      createOrLinkTarget('/api/targets/from-url', { jd_url: 'https://x' })
    ).rejects.toThrow('No experience profile');
  });

  it('falls back to the failure title + status when there is no detail', async () => {
    fetchMock.mockResolvedValue(errorResponse(500));

    await expect(
      createOrLinkTarget('/api/targets/from-manual', { label: 'x' })
    ).rejects.toThrow('Failed to add target (500)');
  });
});

describe('createBareTarget', () => {
  it('POSTs /api/targets and returns the created target', async () => {
    fetchMock.mockResolvedValue(okResponse(TARGET));

    const created = await createBareTarget({ label: 'Staff Engineer' });

    expect(fetchMock).toHaveBeenCalledWith('/api/targets', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label: 'Staff Engineer' }),
    });
    expect(created.id).toBe('t-1');
  });

  it('throws on failure', async () => {
    fetchMock.mockResolvedValue(errorResponse(400, 'label required'));

    await expect(createBareTarget({ label: '' })).rejects.toThrow(
      'label required'
    );
  });
});

describe('linkTarget / linkExistingTarget', () => {
  it('POSTs the link endpoint and returns the raw user-target row', async () => {
    fetchMock.mockResolvedValue(okResponse(USER_TARGET));

    const row = await linkTarget('t-1');

    expect(fetchMock).toHaveBeenCalledWith('/api/targets/t-1/link', {
      method: 'POST',
    });
    expect(row.id).toBe('ut-1');
  });

  it('linkExistingTarget projects to the list-entry shape', async () => {
    fetchMock.mockResolvedValue(okResponse(USER_TARGET));

    const entry = await linkExistingTarget(TARGET);

    expect(entry.user_target.id).toBe('ut-1');
    // Summary projection, not the full target payload.
    expect(entry.target.id).toBe('t-1');
    expect(entry.target.label).toBe('Staff Engineer');
  });

  it('surfaces the 409 active-limit detail', async () => {
    fetchMock.mockResolvedValue(
      errorResponse(409, 'You already have 5 active targets')
    );

    await expect(linkTarget('t-1')).rejects.toThrow(
      'You already have 5 active targets'
    );
  });
});

describe('addSuggestionTarget', () => {
  it('routes brand-new suggestions through from-manual, never link', async () => {
    fetchMock.mockResolvedValue(okResponse(CREATE_RESULT));
    const match = {
      is_new: true,
      suggestion: { label: 'Staff Engineer', description: 'desc' },
      matched_target: null,
    } as unknown as MatchedSuggestion;

    const entry = await addSuggestionTarget(match);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe('/api/targets/from-manual');
    expect(entry.target.id).toBe('t-1');
  });

  it('routes catalog matches through link, never from-manual', async () => {
    fetchMock.mockResolvedValue(okResponse(USER_TARGET));
    const match = {
      is_new: false,
      suggestion: { label: 'Staff Engineer', description: 'desc' },
      matched_target: TARGET,
    } as unknown as MatchedSuggestion;

    const entry = await addSuggestionTarget(match);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe('/api/targets/t-1/link');
    expect(entry.user_target.id).toBe('ut-1');
  });
});

describe('addSearchSuggestionTarget (profile-independent LLM fallback)', () => {
  it('POSTs the pick to the profile-free from-suggestion endpoint (server dedups)', async () => {
    fetchMock.mockResolvedValue(okResponse(CREATE_RESULT));
    const match = {
      is_new: true,
      suggestion: { label: 'Staff Engineer', description: 'desc' },
      matched_target: null,
    } as unknown as MatchedSuggestion;

    const entry = await addSearchSuggestionTarget(match);

    // ONE server call — the API create-or-links + dedups; no client-side dance.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/targets/from-suggestion');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({
      label: 'Staff Engineer',
      description: 'desc',
    });
    // Never the profile-gated /from-manual, and no bare-create/link/activate.
    expect(url).not.toBe('/api/targets/from-manual');
    expect(entry.target.id).toBe('t-1');
    expect(entry.user_target.id).toBe('ut-1');
  });

  it('ignores the stale client is_new — the server re-matches either way', async () => {
    // is_new=false, but the flow still just POSTs the label+description and
    // lets the server decide link-vs-create (dedup is authoritative there).
    fetchMock.mockResolvedValue(okResponse(CREATE_RESULT));
    const match = {
      is_new: false,
      suggestion: { label: 'Staff Engineer', description: 'desc' },
      matched_target: TARGET,
    } as unknown as MatchedSuggestion;

    await addSearchSuggestionTarget(match);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe('/api/targets/from-suggestion');
  });

  it('surfaces the server error on failure', async () => {
    fetchMock.mockResolvedValue(errorResponse(500, 'boom'));
    const match = {
      is_new: true,
      suggestion: { label: 'Staff Engineer', description: 'desc' },
      matched_target: null,
    } as unknown as MatchedSuggestion;

    await expect(addSearchSuggestionTarget(match)).rejects.toThrow('boom');
  });
});

describe('activateTargetInBackground', () => {
  it('fires the activate POST without awaiting', () => {
    fetchMock.mockResolvedValue(okResponse(TARGET));

    activateTargetInBackground('t-1');

    expect(fetchMock).toHaveBeenCalledWith('/api/targets/t-1/activate', {
      method: 'POST',
    });
  });

  it('swallows a rejected kickoff instead of surfacing an unhandled rejection', async () => {
    fetchMock.mockRejectedValue(new Error('network down'));

    expect(() => activateTargetInBackground('t-1')).not.toThrow();
    // Flush the microtask queue — an unhandled rejection here would fail
    // the test run (jest treats it as an error in strict node modes).
    await new Promise(resolve => setTimeout(resolve, 0));
  });
});

describe('toListEntry', () => {
  it('projects the full target to the list summary shape', () => {
    const entry = toListEntry(CREATE_RESULT);
    expect(entry.user_target).toBe(USER_TARGET);
    expect(entry.target.id).toBe('t-1');
  });
});
