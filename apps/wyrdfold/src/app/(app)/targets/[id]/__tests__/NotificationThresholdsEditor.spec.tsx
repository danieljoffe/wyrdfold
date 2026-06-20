import React from 'react';
import '@testing-library/jest-dom';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import NotificationThresholdsEditor, {
  parseThresholdInput,
} from '../NotificationThresholdsEditor';
import type { UserTarget } from '../../types';

const mockToast = jest.fn();
jest.mock('@/state/Toast/ToastProvider', () => ({
  useToast: () => ({ toast: mockToast }),
}));

const ORIGINAL_FETCH = global.fetch;

function makeUserTarget(over: Partial<UserTarget> = {}): UserTarget {
  return {
    id: 'ut-1',
    user_id: 'user-1',
    target_id: 't-1',
    is_active: true,
    fit_score: null,
    fit_score_reasoning: null,
    axis_weights: null,
    axis_weights_previous: null,
    job_score_threshold: null,
    sms_score_threshold: null,
    created_at: '2026-01-01',
    updated_at: '2026-01-01',
    ...over,
  };
}

/**
 * Mock fetch: GET /api/profile/notifications returns the account defaults;
 * PATCH /api/targets/:id/notification-thresholds echoes the requested body
 * back as a UserTarget and records the request for assertions.
 */
function mockFetch(defaults = { job: 100, sms: 90 }) {
  const calls: { url: string; body: unknown }[] = [];
  global.fetch = jest.fn((input: string, init?: RequestInit) => {
    if (input.includes('/profile/notifications')) {
      return Promise.resolve({
        ok: true,
        json: async () => ({
          job_score_threshold: defaults.job,
          sms_score_threshold: defaults.sms,
        }),
      });
    }
    const body = init?.body ? JSON.parse(init.body as string) : null;
    calls.push({ url: input, body });
    return Promise.resolve({
      ok: true,
      json: async () =>
        makeUserTarget({
          job_score_threshold: body?.job_score_threshold ?? null,
          sms_score_threshold: body?.sms_score_threshold ?? null,
        }),
    });
  }) as unknown as typeof fetch;
  return calls;
}

beforeEach(() => {
  jest.clearAllMocks();
});

afterAll(() => {
  global.fetch = ORIGINAL_FETCH;
});

describe('parseThresholdInput', () => {
  it.each([
    ['', { value: null, valid: true }],
    ['  ', { value: null, valid: true }],
    ['0', { value: 0, valid: true }],
    ['80', { value: 80, valid: true }],
    ['100', { value: 100, valid: true }],
    ['101', { value: null, valid: false }],
    ['-1', { value: null, valid: false }],
    ['50.5', { value: null, valid: false }],
    ['abc', { value: null, valid: false }],
  ])('parses %p', (raw, expected) => {
    expect(parseThresholdInput(raw as string)).toEqual(expected);
  });
});

describe('NotificationThresholdsEditor', () => {
  it('shows blank inputs, default badges, and the inherited account hint', async () => {
    mockFetch({ job: 100, sms: 90 });
    render(
      <NotificationThresholdsEditor
        targetId='t-1'
        userTarget={makeUserTarget()}
        onUpdated={jest.fn()}
      />
    );

    const email = screen.getByLabelText(
      /email alerts score threshold/i
    ) as HTMLInputElement;
    const sms = screen.getByLabelText(
      /sms alerts score threshold/i
    ) as HTMLInputElement;
    expect(email.value).toBe('');
    expect(sms.value).toBe('');

    // Two per-channel "Account default" badges.
    expect(
      screen.getAllByText(/account default/i).length
    ).toBeGreaterThanOrEqual(2);
    // Inherited-default hint resolves after the notifications fetch.
    expect(
      await screen.findByText(/your account default \(100\)/i)
    ).toBeInTheDocument();
    expect(
      await screen.findByText(/your account default \(90\)/i)
    ).toBeInTheDocument();
  });

  it('saves both channels, sending an explicit null for the blank one', async () => {
    const calls = mockFetch();
    const onUpdated = jest.fn();
    render(
      <NotificationThresholdsEditor
        targetId='t-1'
        userTarget={makeUserTarget()}
        onUpdated={onUpdated}
      />
    );

    fireEvent.change(screen.getByLabelText(/email alerts score threshold/i), {
      target: { value: '80' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(onUpdated).toHaveBeenCalledTimes(1));
    const patch = calls.find(c => c.url.includes('/notification-thresholds'));
    expect(patch?.body).toEqual({
      job_score_threshold: 80,
      sms_score_threshold: null,
    });
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({ variant: 'success' })
    );
  });

  it('blocks save and shows an error for an out-of-range value', async () => {
    mockFetch();
    render(
      <NotificationThresholdsEditor
        targetId='t-1'
        userTarget={makeUserTarget()}
        onUpdated={jest.fn()}
      />
    );

    fireEvent.change(screen.getByLabelText(/email alerts score threshold/i), {
      target: { value: '250' },
    });

    expect(screen.getByText(/whole number 0/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^save$/i })).toBeDisabled();
  });

  it('reset sends explicit nulls for both channels', async () => {
    const calls = mockFetch();
    const onUpdated = jest.fn();
    render(
      <NotificationThresholdsEditor
        targetId='t-1'
        userTarget={makeUserTarget({ job_score_threshold: 80 })}
        onUpdated={onUpdated}
      />
    );

    fireEvent.click(
      screen.getByRole('button', { name: /use account defaults/i })
    );

    await waitFor(() => expect(onUpdated).toHaveBeenCalledTimes(1));
    const patch = calls.find(c => c.url.includes('/notification-thresholds'));
    expect(patch?.body).toEqual({
      job_score_threshold: null,
      sms_score_threshold: null,
    });
  });

  it('disables the reset button when nothing is customised', () => {
    mockFetch();
    render(
      <NotificationThresholdsEditor
        targetId='t-1'
        userTarget={makeUserTarget()}
        onUpdated={jest.fn()}
      />
    );
    expect(
      screen.getByRole('button', { name: /use account defaults/i })
    ).toBeDisabled();
  });
});
