import React from 'react';
import '@testing-library/jest-dom';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import TargetPreferencesEditor, {
  DEFAULT_PREFERENCES,
  parseList,
  parseScoreCutoff,
  SCORE_MAX,
  type TargetPreferences,
} from '../TargetPreferencesEditor';

const mockToast = jest.fn();
jest.mock('@/state/Toast/ToastProvider', () => ({
  useToast: () => ({ toast: mockToast }),
}));

const ORIGINAL_FETCH = global.fetch;

/**
 * GET /api/targets/:id/preferences returns the current prefs; PUT echoes the
 * request body back (the API returns the stored TargetPreferences) and records
 * the call for assertions.
 */
function mockFetch(initial: Partial<TargetPreferences> = {}) {
  const prefs: TargetPreferences = { ...DEFAULT_PREFERENCES, ...initial };
  const calls: { url: string; method: string; body: unknown }[] = [];
  global.fetch = jest.fn((input: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET';
    if (method === 'GET') {
      return Promise.resolve({ ok: true, json: async () => prefs });
    }
    const body = init?.body ? JSON.parse(init.body as string) : null;
    calls.push({ url: input, method, body });
    return Promise.resolve({ ok: true, json: async () => body });
  }) as unknown as typeof fetch;
  return calls;
}

beforeEach(() => jest.clearAllMocks());
afterAll(() => {
  global.fetch = ORIGINAL_FETCH;
});

describe('parseScoreCutoff', () => {
  // The ceiling is 100, not 200. Job scores are hard-clamped to 0-100 at the
  // write site (api services/scoring.py: `max(0, min(100, ...))`), so a cutoff
  // above 100 could only ever hide EVERY job — silently, with nothing in the
  // UI explaining the now-empty list. 150 saved cleanly before this change.
  it.each([
    ['', { value: 40, valid: true }],
    ['0', { value: 0, valid: true }],
    ['100', { value: 100, valid: true }],
    ['101', { value: 40, valid: false }],
    ['150', { value: 40, valid: false }],
    ['200', { value: 40, valid: false }],
    ['-1', { value: 40, valid: false }],
    ['40.5', { value: 40, valid: false }],
    ['abc', { value: 40, valid: false }],
  ])('parses %p', (raw, expected) => {
    expect(parseScoreCutoff(raw as string)).toEqual(expected);
  });

  it('caps at the same ceiling the score scale actually has', () => {
    expect(SCORE_MAX).toBe(100);
    // Guards the regression directly: any value above the real ceiling is
    // rejected rather than accepted-then-silently-filtering-everything.
    expect(parseScoreCutoff(String(SCORE_MAX + 1)).valid).toBe(false);
  });
});

describe('parseList', () => {
  it('splits comma/newline, trims, and maps empty → null', () => {
    expect(parseList('   ')).toBeNull();
    expect(parseList('New York, Remote')).toEqual(['New York', 'Remote']);
    expect(parseList('a\nb , c')).toEqual(['a', 'b', 'c']);
  });
});

describe('TargetPreferencesEditor', () => {
  it('loads the saved preferences and hydrates the form', async () => {
    mockFetch({ pref_score_cutoff: 60, pref_locations: ['Remote'] });
    render(<TargetPreferencesEditor targetId='t-1' />);
    const cutoff = (await screen.findByLabelText(
      /minimum fit score/i
    )) as HTMLInputElement;
    expect(cutoff.value).toBe('60');
    expect(
      (screen.getByLabelText(/^locations$/i) as HTMLInputElement).value
    ).toBe('Remote');
  });

  it('PUTs the full preference set on save', async () => {
    const calls = mockFetch();
    render(<TargetPreferencesEditor targetId='t-1' />);
    await screen.findByLabelText(/minimum fit score/i);

    fireEvent.change(screen.getByLabelText(/minimum fit score/i), {
      target: { value: '70' },
    });
    fireEvent.change(screen.getByLabelText(/^locations$/i), {
      target: { value: 'Austin, Remote' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(calls.length).toBe(1));
    expect(calls[0].method).toBe('PUT');
    expect(calls[0].body).toEqual({
      pref_score_cutoff: 70,
      pref_locations: ['Austin', 'Remote'],
      pref_remote_ok: true,
      pref_seniority_min: null,
      pref_seniority_max: null,
      pref_employment_types: null,
      pref_include_unknown_salary: true,
    });
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({ variant: 'success' })
    );
  });

  it('blocks save and shows an error for an out-of-range cutoff', async () => {
    mockFetch();
    render(<TargetPreferencesEditor targetId='t-1' />);
    await screen.findByLabelText(/minimum fit score/i);

    fireEvent.change(screen.getByLabelText(/minimum fit score/i), {
      target: { value: '999' },
    });
    expect(screen.getByText(/whole number 0/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^save$/i })).toBeDisabled();
  });
});
