import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import SettingsPage from '../SettingsPage';

const mockToast = jest.fn();
jest.mock('@/state/Toast/ToastProvider', () => ({
  useToast: () => ({ toast: mockToast }),
}));

const mockReplace = jest.fn();
let mockSearch = '';
jest.mock('next/navigation', () => ({
  useRouter: () => ({
    push: jest.fn(),
    prefetch: jest.fn(),
    replace: mockReplace,
  }),
  usePathname: () => '/settings',
  useSearchParams: () => new URLSearchParams(mockSearch),
}));

// The Account tab's admin cards fetch their own data — stub them so this
// spec stays about the page's tab shell + notification/preference forms.
jest.mock(
  '../ApiKeysCard',
  () =>
    function ApiKeysCardStub() {
      return <div data-testid='api-keys-card' />;
    }
);
jest.mock(
  '../LlmUsageCard',
  () =>
    function LlmUsageCardStub() {
      return <div data-testid='llm-usage-card' />;
    }
);
jest.mock(
  '../BillingCard',
  () =>
    function BillingCardStub() {
      return <div data-testid='billing-card' />;
    }
);
jest.mock(
  '../DataExportCard',
  () =>
    function DataExportCardStub() {
      return <div data-testid='data-export-card' />;
    }
);
jest.mock(
  '../OnboardingResetCard',
  () =>
    function OnboardingResetCardStub() {
      return <div data-testid='onboarding-reset-card' />;
    }
);
jest.mock(
  '../DeleteAccountCard',
  () =>
    function DeleteAccountCardStub() {
      return <div data-testid='delete-account-card' />;
    }
);

const NOTIFICATIONS = {
  job_notifications_enabled: false,
  job_score_threshold: 80,
  sms_notifications_enabled: false,
  sms_score_threshold: 90,
  sms_daily_limit: 5,
  list_min_score: null,
  // Widened: the SMS-card cases override this with a real number.
  phone_number: null as string | null,
  email: 'me@example.com',
  email_available: true,
  sms_available: true,
};

const RESUME_STYLE = { preset: 'modern', accent: 'slate' };

const originalFetch = global.fetch;

interface FetchInit {
  method?: string;
  body?: string;
}

function mockFetchWith(notifications: typeof NOTIFICATIONS) {
  global.fetch = jest
    .fn()
    .mockImplementation((url: string, init?: FetchInit) => {
      if (url.includes('/resume-style')) {
        // Echo PATCH bodies so the optimistic UI keeps the chosen value.
        if (init?.method === 'PATCH' && init.body) {
          const patched = JSON.parse(init.body) as Record<string, unknown>;
          return Promise.resolve({
            ok: true,
            json: async () => ({ ...RESUME_STYLE, ...patched }),
          } as Response);
        }
        return Promise.resolve({
          ok: true,
          json: async () => RESUME_STYLE,
        } as Response);
      }
      if (url.includes('/notifications')) {
        if (init?.method === 'PATCH' && init.body) {
          const patched = JSON.parse(init.body) as Record<string, unknown>;
          return Promise.resolve({
            ok: true,
            json: async () => ({ ...notifications, ...patched }),
          } as Response);
        }
        return Promise.resolve({
          ok: true,
          json: async () => notifications,
        } as Response);
      }
      return Promise.resolve({ ok: false, json: async () => ({}) } as Response);
    }) as unknown as typeof fetch;
}

beforeEach(() => {
  mockToast.mockReset();
  mockReplace.mockReset();
  mockSearch = '';
  mockFetchWith(NOTIFICATIONS);
});

afterEach(() => {
  global.fetch = originalFetch;
});

describe('SettingsPage', () => {
  it('groups the cards under three tabs, Preferences first', async () => {
    render(<SettingsPage />);
    expect(await screen.findByText(/export style/i)).toBeInTheDocument();

    for (const name of ['Preferences', 'Notifications', 'Account']) {
      expect(screen.getByRole('tab', { name })).toBeInTheDocument();
    }
    // Default tab = Preferences: its cards render, the others' don't.
    expect(screen.getByText(/score threshold/i)).toBeInTheDocument();
    expect(screen.queryByText(/email notifications/i)).toBeNull();
    expect(screen.queryByTestId('billing-card')).toBeNull();
  });

  it('Notifications tab shows both channels and writes ?tab= to the URL', async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    await screen.findByText(/export style/i);

    await user.click(screen.getByRole('tab', { name: 'Notifications' }));

    expect(await screen.findByText(/email notifications/i)).toBeInTheDocument();
    expect(screen.getByText(/sms notifications/i)).toBeInTheDocument();
    expect(mockReplace).toHaveBeenCalledWith('/settings?tab=notifications', {
      scroll: false,
    });

    // Returning to the default tab cleans the param off the URL.
    await user.click(screen.getByRole('tab', { name: 'Preferences' }));
    expect(mockReplace).toHaveBeenLastCalledWith('/settings', {
      scroll: false,
    });
  });

  it('deep-links a non-default tab from ?tab=', async () => {
    mockSearch = 'tab=account';
    render(<SettingsPage />);

    expect(await screen.findByTestId('billing-card')).toBeInTheDocument();
    expect(screen.getByTestId('delete-account-card')).toBeInTheDocument();
    expect(screen.queryByText(/export style/i)).toBeNull();
  });

  it('SMS card has no phone input — the Profile record is displayed instead', async () => {
    mockFetchWith({
      ...NOTIFICATIONS,
      phone_number: '+15555550100',
      sms_notifications_enabled: true,
    });
    mockSearch = 'tab=notifications';
    const user = userEvent.setup();
    render(<SettingsPage />);

    expect(await screen.findByText(/sms notifications/i)).toBeInTheDocument();
    // The second phone writer is gone (Profile owns the contact record) …
    expect(screen.queryByLabelText(/phone number/i)).toBeNull();
    // … and the on-file number is shown read-only with a Profile link.
    expect(screen.getByText('+15555550100')).toBeInTheDocument();
    const profileLinks = screen.getAllByRole('link', {
      name: /managed on your profile/i,
    });
    expect(profileLinks.length).toBeGreaterThan(0);
    expect(profileLinks[0]).toHaveAttribute('href', '/profile');

    // Saving SMS settings never writes phone_number.
    const dailyLimit = screen.getByLabelText(/daily limit/i);
    await user.clear(dailyLimit);
    await user.type(dailyLimit, '7');
    await waitFor(
      () => {
        const calls = (global.fetch as jest.Mock).mock.calls as [
          string,
          FetchInit?,
        ][];
        const patch = calls.find(
          ([url, init]) =>
            String(url).includes('/notifications') && init?.method === 'PATCH'
        );
        expect(patch).toBeTruthy();
        const body = JSON.parse(patch?.[1]?.body ?? '{}') as Record<
          string,
          unknown
        >;
        expect(Object.keys(body)).not.toContain('phone_number');
        expect(body.sms_daily_limit).toBe(7);
      },
      { timeout: 2000 }
    );
  });

  it('SMS card points at Profile when no phone is on file', async () => {
    mockSearch = 'tab=notifications';
    render(<SettingsPage />);

    expect(await screen.findByText(/sms notifications/i)).toBeInTheDocument();
    expect(screen.getByText(/no phone number on file/i)).toBeInTheDocument();
    expect(
      screen.getByRole('link', { name: /add one on your profile/i })
    ).toHaveAttribute('href', '/profile');
  });

  it('renders the export style card with the saved preset/accent and a preview', async () => {
    render(<SettingsPage />);
    expect(await screen.findByText(/export style/i)).toBeInTheDocument();
    const presetSelect = screen.getByLabelText(/^Style$/i) as HTMLSelectElement;
    const accentSelect = screen.getByLabelText(
      /accent color/i
    ) as HTMLSelectElement;
    expect(presetSelect.value).toBe('modern');
    expect(accentSelect.value).toBe('slate');
    expect(
      screen.getByRole('img', { name: /export style preview/i })
    ).toBeInTheDocument();
  });

  it('autosaves a PATCH when the preset changes', async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    const presetSelect = await screen.findByLabelText(/^Style$/i);

    await user.selectOptions(presetSelect, 'classic');

    await waitFor(
      () => {
        const calls = (global.fetch as jest.Mock).mock.calls as [
          string,
          FetchInit?,
        ][];
        const patchCall = calls.find(
          ([url, init]) =>
            String(url).includes('/resume-style') && init?.method === 'PATCH'
        );
        expect(patchCall?.[1]?.body).toBeTruthy();
        expect(JSON.parse(patchCall?.[1]?.body ?? '{}')).toEqual({
          preset: 'classic',
          accent: 'slate',
        });
      },
      { timeout: 2000 }
    );
  });

  it('an edit still autosaves after switching away from the tab', async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    const presetSelect = await screen.findByLabelText(/^Style$/i);

    await user.selectOptions(presetSelect, 'classic');
    // Flip tabs before the debounce fires — the panel unmounts but the
    // page-level autosave effect must still deliver the PATCH.
    await user.click(screen.getByRole('tab', { name: 'Account' }));

    await waitFor(
      () => {
        const calls = (global.fetch as jest.Mock).mock.calls as [
          string,
          FetchInit?,
        ][];
        const patchCall = calls.find(
          ([url, init]) =>
            String(url).includes('/resume-style') && init?.method === 'PATCH'
        );
        expect(patchCall?.[1]?.body).toBeTruthy();
      },
      { timeout: 2000 }
    );
  });
});
