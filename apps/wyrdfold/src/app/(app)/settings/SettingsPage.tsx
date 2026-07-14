'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@danieljoffe/shared-ui/Card';
import { Heading } from '@danieljoffe/shared-ui/Heading';
import { Input } from '@danieljoffe/shared-ui/Input';
import { Select } from '@danieljoffe/shared-ui/Select';
import { Skeleton } from '@danieljoffe/shared-ui/Skeleton';
import { Spinner } from '@danieljoffe/shared-ui/Spinner';
import { Switch } from '@danieljoffe/shared-ui/Switch';
import { Tabs, type Tab } from '@danieljoffe/shared-ui/Tabs';
import { Text } from '@danieljoffe/shared-ui/Text';
import { extractApiError } from '@/lib/extractApiError';
import { useToast } from '@/state/Toast/ToastProvider';
import ApiKeysCard from './ApiKeysCard';
import DataExportCard from './DataExportCard';
import DeleteAccountCard from './DeleteAccountCard';
import BillingCard from './BillingCard';
import LlmUsageCard from './LlmUsageCard';
import OnboardingResetCard from './OnboardingResetCard';
import { ResumeStylePreview } from './ResumeStylePreview';
import {
  ACCENT_OPTIONS,
  DEFAULT_RESUME_STYLE,
  PRESET_OPTIONS,
  type ResumeStyleAccent,
  type ResumeStylePreset,
  type ResumeStyleSettings,
} from './resumeStyle';

const AUTOSAVE_DEBOUNCE_MS = 800;

/**
 * The former 10-card stack is grouped by scope (UX/IA §2.2-2.3):
 * ``Preferences`` (how output + the list behave) · ``Notifications``
 * (alert channels) · ``Account`` (keys, plan, data, danger zone).
 * ``?tab=`` keeps the choice shareable and sticky across reloads
 * (``router.replace`` — tab flips shouldn't pile up history entries).
 *
 * Contact is a single record: ``user_profiles.phone_number`` / ``email``
 * are edited on /profile (resume header) and only *displayed* here as the
 * alert destinations — Settings no longer carries a second phone writer
 * that could silently overwrite the Profile one.
 */
const TAB_IDS = ['preferences', 'notifications', 'account'] as const;
type SettingsTabId = (typeof TAB_IDS)[number];

function parseTab(raw: string | null): SettingsTabId {
  return (TAB_IDS as readonly string[]).includes(raw ?? '')
    ? (raw as SettingsTabId)
    : 'preferences';
}

interface NotificationPreferences {
  job_notifications_enabled: boolean;
  job_score_threshold: number;
  sms_notifications_enabled: boolean;
  sms_score_threshold: number;
  sms_daily_limit: number;
  list_min_score: number | null;
  phone_number: string | null;
  email: string | null;
  email_available: boolean;
  sms_available: boolean;
}

type Section = 'list' | 'email' | 'sms';

function emailSig(enabled: boolean, threshold: number): string {
  return JSON.stringify({
    job_notifications_enabled: enabled,
    job_score_threshold: threshold,
  });
}

function smsSig(
  enabled: boolean,
  threshold: number,
  dailyLimit: number
): string {
  return JSON.stringify({
    sms_notifications_enabled: enabled,
    sms_score_threshold: threshold,
    sms_daily_limit: dailyLimit,
  });
}

function listSig(value: number | null): string {
  return JSON.stringify({ list_min_score: value });
}

function styleSig(
  preset: ResumeStylePreset,
  accent: ResumeStyleAccent
): string {
  return JSON.stringify({ preset, accent });
}

function SavingIndicator({ active }: { active: boolean }) {
  if (!active) return null;
  return (
    <Text
      as='span'
      variant='meta'
      className='inline-flex items-center gap-1'
      aria-live='polite'
    >
      <Spinner size='sm' aria-label='Saving' />
      <span>Saving…</span>
    </Text>
  );
}

// Parses the score-threshold input. Empty / non-numeric / 0 → null
// (semantic clear — caller wants no floor). Otherwise clamps to 0-100.
function parseListThreshold(raw: string): number | null {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  const n = parseInt(trimmed, 10);
  if (Number.isNaN(n) || n <= 0) return null;
  return Math.min(100, Math.max(0, n));
}

export default function SettingsPage() {
  const [loading, setLoading] = useState(true);
  const [savingSection, setSavingSection] = useState<Section | null>(null);
  const [prefs, setPrefs] = useState<NotificationPreferences | null>(null);
  const { toast } = useToast();

  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  // The shared Tabs component is uncontrolled; remember what the URL said
  // on first render so the two never disagree about the initial tab. No
  // activeTab state — nothing here renders differently by tab (unlike
  // TargetDetail's lazy JD fetch); the URL is the only consumer.
  const initialTabRef = useRef(parseTab(searchParams.get('tab')));

  const handleTabChange = useCallback(
    (tabId: string) => {
      const tab = parseTab(tabId);
      const params = new URLSearchParams(searchParams.toString());
      if (tab === 'preferences') {
        params.delete('tab');
      } else {
        params.set('tab', tab);
      }
      const qs = params.toString();
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    },
    [pathname, router, searchParams]
  );

  // Form state
  const [emailEnabled, setEmailEnabled] = useState(false);
  const [emailThreshold, setEmailThreshold] = useState('100');
  const [smsEnabled, setSmsEnabled] = useState(false);
  const [smsThreshold, setSmsThreshold] = useState('100');
  const [smsDailyLimit, setSmsDailyLimit] = useState('5');
  const [listMinScoreRaw, setListMinScoreRaw] = useState('');
  const [savingStyle, setSavingStyle] = useState(false);
  const [stylePreset, setStylePreset] = useState<ResumeStylePreset>(
    DEFAULT_RESUME_STYLE.preset
  );
  const [styleAccent, setStyleAccent] = useState<ResumeStyleAccent>(
    DEFAULT_RESUME_STYLE.accent
  );

  // Server-known signatures — autosave fires only when local state diverges.
  // Failed-sigs prevent retry-loops when the server rejects a value.
  const lastEmailSigRef = useRef<string | null>(null);
  const lastSmsSigRef = useRef<string | null>(null);
  const lastListSigRef = useRef<string | null>(null);
  const lastFailedEmailSigRef = useRef<string | null>(null);
  const lastFailedSmsSigRef = useRef<string | null>(null);
  const lastFailedListSigRef = useRef<string | null>(null);
  const lastStyleSigRef = useRef<string | null>(null);
  const lastFailedStyleSigRef = useRef<string | null>(null);

  const fetchPrefs = useCallback(async () => {
    try {
      const [prefsRes, styleRes] = await Promise.all([
        fetch('/api/profile/notifications'),
        fetch('/api/profile/resume-style'),
      ]);
      if (prefsRes.ok) {
        const data = (await prefsRes.json()) as NotificationPreferences;
        setPrefs(data);
        setEmailEnabled(data.job_notifications_enabled);
        setEmailThreshold(String(data.job_score_threshold));
        setSmsEnabled(data.sms_notifications_enabled);
        setSmsThreshold(String(data.sms_score_threshold));
        setSmsDailyLimit(String(data.sms_daily_limit));
        setListMinScoreRaw(
          data.list_min_score !== null && data.list_min_score !== undefined
            ? String(data.list_min_score)
            : ''
        );
        lastEmailSigRef.current = emailSig(
          data.job_notifications_enabled,
          data.job_score_threshold
        );
        lastSmsSigRef.current = smsSig(
          data.sms_notifications_enabled,
          data.sms_score_threshold,
          data.sms_daily_limit
        );
        lastListSigRef.current = listSig(data.list_min_score ?? null);
      }
      if (styleRes.ok) {
        const style = (await styleRes.json()) as ResumeStyleSettings;
        setStylePreset(style.preset);
        setStyleAccent(style.accent);
        lastStyleSigRef.current = styleSig(style.preset, style.accent);
      }
    } catch {
      toast({
        variant: 'error',
        title: 'Failed to load settings',
      });
    } finally {
      setLoading(false);
    }
  }, [toast]);

  useEffect(() => {
    fetchPrefs();
  }, [fetchPrefs]);

  // -- Save handlers ----------------------------------------------------------

  const handleSaveList = useCallback(async () => {
    const parsed = parseListThreshold(listMinScoreRaw);
    const sig = listSig(parsed);
    setSavingSection('list');
    try {
      // ``null`` → clear semantics — send 0 to indicate "no floor" per the
      // backend contract (the API rejects negative values and treats 0 as
      // "remove the default-min-score filter").
      const res = await fetch('/api/profile/notifications', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ list_min_score: parsed ?? 0 }),
      });
      if (!res.ok) {
        const message = await extractApiError(
          res,
          'Failed to save score threshold'
        );
        toast({ variant: 'error', title: message });
        lastFailedListSigRef.current = sig;
        return;
      }
      const data = (await res.json()) as NotificationPreferences;
      setPrefs(data);
      setListMinScoreRaw(
        data.list_min_score !== null && data.list_min_score !== undefined
          ? String(data.list_min_score)
          : ''
      );
      lastListSigRef.current = listSig(data.list_min_score ?? null);
      lastFailedListSigRef.current = null;
      toast({ variant: 'success', title: 'Score threshold saved' });
    } catch {
      toast({ variant: 'error', title: 'Failed to save score threshold' });
      lastFailedListSigRef.current = sig;
    } finally {
      setSavingSection(null);
    }
  }, [listMinScoreRaw, toast]);

  const handleSaveEmail = useCallback(async () => {
    const threshold = parseInt(emailThreshold, 10) || 100;
    const sig = emailSig(emailEnabled, threshold);
    setSavingSection('email');
    try {
      const res = await fetch('/api/profile/notifications', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          job_notifications_enabled: emailEnabled,
          job_score_threshold: threshold,
        }),
      });
      if (!res.ok) {
        const message = await extractApiError(
          res,
          'Failed to save email settings'
        );
        toast({ variant: 'error', title: message });
        lastFailedEmailSigRef.current = sig;
        return;
      }
      const data = (await res.json()) as NotificationPreferences;
      setPrefs(data);
      setEmailEnabled(data.job_notifications_enabled);
      setEmailThreshold(String(data.job_score_threshold));
      lastEmailSigRef.current = emailSig(
        data.job_notifications_enabled,
        data.job_score_threshold
      );
      lastFailedEmailSigRef.current = null;
      toast({ variant: 'success', title: 'Email settings saved' });
    } catch {
      toast({ variant: 'error', title: 'Failed to save email settings' });
      lastFailedEmailSigRef.current = sig;
    } finally {
      setSavingSection(null);
    }
  }, [emailEnabled, emailThreshold, toast]);

  const handleSaveSms = useCallback(async () => {
    const threshold = parseInt(smsThreshold, 10) || 100;
    const dailyLimit = parseInt(smsDailyLimit, 10) || 5;
    const sig = smsSig(smsEnabled, threshold, dailyLimit);
    setSavingSection('sms');
    try {
      const res = await fetch('/api/profile/notifications', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          sms_notifications_enabled: smsEnabled,
          sms_score_threshold: threshold,
          sms_daily_limit: dailyLimit,
        }),
      });
      if (!res.ok) {
        const message = await extractApiError(
          res,
          'Failed to save SMS settings'
        );
        toast({ variant: 'error', title: message });
        lastFailedSmsSigRef.current = sig;
        return;
      }
      const data = (await res.json()) as NotificationPreferences;
      setPrefs(data);
      setSmsEnabled(data.sms_notifications_enabled);
      setSmsThreshold(String(data.sms_score_threshold));
      setSmsDailyLimit(String(data.sms_daily_limit));
      lastSmsSigRef.current = smsSig(
        data.sms_notifications_enabled,
        data.sms_score_threshold,
        data.sms_daily_limit
      );
      lastFailedSmsSigRef.current = null;
      toast({ variant: 'success', title: 'SMS settings saved' });
    } catch {
      toast({ variant: 'error', title: 'Failed to save SMS settings' });
      lastFailedSmsSigRef.current = sig;
    } finally {
      setSavingSection(null);
    }
  }, [smsEnabled, smsThreshold, smsDailyLimit, toast]);

  const handleSaveStyle = useCallback(async () => {
    const sig = styleSig(stylePreset, styleAccent);
    setSavingStyle(true);
    try {
      const res = await fetch('/api/profile/resume-style', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ preset: stylePreset, accent: styleAccent }),
      });
      if (!res.ok) {
        const message = await extractApiError(
          res,
          'Failed to save export style'
        );
        toast({ variant: 'error', title: message });
        lastFailedStyleSigRef.current = sig;
        return;
      }
      const data = (await res.json()) as ResumeStyleSettings;
      setStylePreset(data.preset);
      setStyleAccent(data.accent);
      lastStyleSigRef.current = styleSig(data.preset, data.accent);
      lastFailedStyleSigRef.current = null;
      toast({ variant: 'success', title: 'Export style saved' });
    } catch {
      toast({ variant: 'error', title: 'Failed to save export style' });
      lastFailedStyleSigRef.current = sig;
    } finally {
      setSavingStyle(false);
    }
  }, [stylePreset, styleAccent, toast]);

  // -- Autosave effects -------------------------------------------------------
  // These live at page level (not per tab panel), so an edit followed by a
  // tab switch still saves — the debounce survives the panel unmounting.

  useEffect(() => {
    if (lastListSigRef.current === null) return;
    if (savingSection === 'list') return;
    const sig = listSig(parseListThreshold(listMinScoreRaw));
    if (sig === lastListSigRef.current) return;
    if (sig === lastFailedListSigRef.current) return;
    const handle = setTimeout(() => {
      handleSaveList();
    }, AUTOSAVE_DEBOUNCE_MS);
    return () => clearTimeout(handle);
  }, [listMinScoreRaw, savingSection, handleSaveList]);

  useEffect(() => {
    if (lastEmailSigRef.current === null) return;
    if (savingSection === 'email') return;
    const sig = emailSig(emailEnabled, parseInt(emailThreshold, 10) || 100);
    if (sig === lastEmailSigRef.current) return;
    if (sig === lastFailedEmailSigRef.current) return;
    const handle = setTimeout(() => {
      handleSaveEmail();
    }, AUTOSAVE_DEBOUNCE_MS);
    return () => clearTimeout(handle);
  }, [emailEnabled, emailThreshold, savingSection, handleSaveEmail]);

  useEffect(() => {
    if (lastSmsSigRef.current === null) return;
    if (savingSection === 'sms') return;
    const sig = smsSig(
      smsEnabled,
      parseInt(smsThreshold, 10) || 100,
      parseInt(smsDailyLimit, 10) || 5
    );
    if (sig === lastSmsSigRef.current) return;
    if (sig === lastFailedSmsSigRef.current) return;
    const handle = setTimeout(() => {
      handleSaveSms();
    }, AUTOSAVE_DEBOUNCE_MS);
    return () => clearTimeout(handle);
  }, [smsEnabled, smsThreshold, smsDailyLimit, savingSection, handleSaveSms]);

  useEffect(() => {
    if (lastStyleSigRef.current === null) return;
    if (savingStyle) return;
    const sig = styleSig(stylePreset, styleAccent);
    if (sig === lastStyleSigRef.current) return;
    if (sig === lastFailedStyleSigRef.current) return;
    const handle = setTimeout(() => {
      handleSaveStyle();
    }, AUTOSAVE_DEBOUNCE_MS);
    return () => clearTimeout(handle);
  }, [stylePreset, styleAccent, savingStyle, handleSaveStyle]);

  const emailAvailable = prefs?.email_available ?? false;
  const smsAvailable = prefs?.sms_available ?? false;
  const phoneOnFile = prefs?.phone_number ?? null;

  if (loading) {
    return (
      <div className='flex flex-col gap-6'>
        <div>
          <Skeleton variant='text' size='lg' className='w-32' />
          <Skeleton variant='text' className='mt-2 w-56' />
        </div>
        <Skeleton variant='rectangular' height={300} />
        <Skeleton variant='rectangular' height={250} />
      </div>
    );
  }

  const tabs: Tab[] = [
    {
      id: 'preferences',
      label: 'Preferences',
      content: (
        <div className='flex flex-col gap-6 pt-4'>
          {/* Export style */}
          <Card>
            <CardHeader>
              <div className='flex items-center gap-3'>
                <CardTitle>Export style</CardTitle>
                <SavingIndicator active={savingStyle} />
              </div>
            </CardHeader>
            <CardContent className='flex flex-col gap-4'>
              <Text variant='caption' className='text-text-secondary'>
                Pick how your tailored resume and cover-letter .docx exports
                look. Applies to every new download — no regeneration needed.
              </Text>
              <div className='grid gap-4 sm:grid-cols-2'>
                <Select
                  label='Style'
                  value={stylePreset}
                  onChange={e =>
                    setStylePreset(e.target.value as ResumeStylePreset)
                  }
                  options={PRESET_OPTIONS}
                />
                <Select
                  label='Accent color'
                  value={styleAccent}
                  onChange={e =>
                    setStyleAccent(e.target.value as ResumeStyleAccent)
                  }
                  options={ACCENT_OPTIONS}
                />
              </div>
              <ResumeStylePreview preset={stylePreset} accent={styleAccent} />
            </CardContent>
          </Card>

          {/* Score threshold (jobs-list default filter) */}
          <Card>
            <CardHeader>
              <div className='flex items-center gap-3'>
                <CardTitle>Score threshold</CardTitle>
                <SavingIndicator active={savingSection === 'list'} />
              </div>
            </CardHeader>
            <CardContent className='flex flex-col gap-4'>
              <Text variant='caption' className='text-text-secondary'>
                Hide jobs scoring below this value from the list. Leave empty to
                show everything — your chip filters still work. Independent of
                email and SMS notification thresholds.
              </Text>
              <div className='max-w-xs'>
                <Input
                  label='Minimum score'
                  type='number'
                  value={listMinScoreRaw}
                  onChange={e => setListMinScoreRaw(e.target.value)}
                  min={0}
                  max={100}
                  placeholder='No filter'
                  helperText='0 or empty = show all jobs'
                />
              </div>
            </CardContent>
          </Card>
        </div>
      ),
    },
    {
      id: 'notifications',
      label: 'Notifications',
      content: (
        <div className='flex flex-col gap-6 pt-4'>
          {/* Email Notifications */}
          <Card>
            <CardHeader>
              <div className='flex flex-wrap items-center justify-between gap-3'>
                <div className='flex items-center gap-3'>
                  <CardTitle>Email Notifications</CardTitle>
                  <SavingIndicator active={savingSection === 'email'} />
                </div>
                <Switch
                  checked={emailEnabled && emailAvailable}
                  onChange={setEmailEnabled}
                  label='Enabled'
                  disabled={!emailAvailable}
                />
              </div>
            </CardHeader>
            <CardContent className='flex flex-col gap-4'>
              <Text variant='caption' className='text-text-secondary'>
                Get email alerts when new jobs score above your threshold.
                Powered by Resend.
              </Text>
              {!emailAvailable && (
                <Text variant='meta' className='text-text-tertiary'>
                  Email notifications are unavailable until the operator
                  configures the email provider credentials.
                </Text>
              )}
              {emailAvailable && prefs?.email && (
                <Text variant='meta' className='text-text-tertiary'>
                  Sending to: {prefs.email} —{' '}
                  <Link
                    href='/profile'
                    className='text-brand-500 hover:underline'
                  >
                    managed on your Profile
                  </Link>
                </Text>
              )}
              <div className='max-w-xs'>
                <Input
                  label='Score threshold'
                  type='number'
                  value={emailThreshold}
                  onChange={e => setEmailThreshold(e.target.value)}
                  min={0}
                  max={100}
                  helperText='Minimum job score to trigger an email alert (0-100)'
                  disabled={!emailEnabled || !emailAvailable}
                />
              </div>
            </CardContent>
          </Card>

          {/* SMS Notifications — the phone number is the Profile contact
              record (one writer, /profile); here it is display-only. */}
          <Card>
            <CardHeader>
              <div className='flex flex-wrap items-center justify-between gap-3'>
                <div className='flex items-center gap-3'>
                  <CardTitle>SMS Notifications</CardTitle>
                  <SavingIndicator active={savingSection === 'sms'} />
                </div>
                <Switch
                  checked={smsEnabled && smsAvailable}
                  onChange={setSmsEnabled}
                  label='Enabled'
                  disabled={!smsAvailable}
                />
              </div>
            </CardHeader>
            <CardContent className='flex flex-col gap-4'>
              <Text variant='caption' className='text-text-secondary'>
                Get text messages for high-scoring jobs with a deep link to view
                and act immediately. Powered by Twilio.
              </Text>
              {!smsAvailable && (
                <Text variant='meta' className='text-text-tertiary'>
                  SMS notifications are unavailable until the operator
                  configures Twilio credentials.
                </Text>
              )}
              <Text variant='meta' className='text-text-tertiary'>
                {phoneOnFile ? (
                  <>
                    Texts go to: <span data-sentry-mask>{phoneOnFile}</span> —{' '}
                    <Link
                      href='/profile'
                      className='text-brand-500 hover:underline'
                    >
                      managed on your Profile
                    </Link>
                  </>
                ) : (
                  <>
                    No phone number on file —{' '}
                    <Link
                      href='/profile'
                      className='text-brand-500 hover:underline'
                    >
                      add one on your Profile
                    </Link>{' '}
                    to receive texts.
                  </>
                )}
              </Text>
              <div className='grid gap-4 sm:grid-cols-2'>
                <Input
                  label='Score threshold'
                  type='number'
                  value={smsThreshold}
                  onChange={e => setSmsThreshold(e.target.value)}
                  min={0}
                  max={100}
                  helperText='Minimum score for SMS (0-100)'
                  disabled={!smsEnabled || !smsAvailable}
                />
                <Input
                  label='Daily limit'
                  type='number'
                  value={smsDailyLimit}
                  onChange={e => setSmsDailyLimit(e.target.value)}
                  min={1}
                  max={50}
                  helperText='Max texts per day (1-50)'
                  disabled={!smsEnabled || !smsAvailable}
                />
              </div>
            </CardContent>
          </Card>
        </div>
      ),
    },
    {
      id: 'account',
      label: 'Account',
      content: (
        <div className='flex flex-col gap-6 pt-4'>
          <ApiKeysCard />
          <LlmUsageCard />
          <BillingCard />
          <DataExportCard />
          <OnboardingResetCard />
          <DeleteAccountCard />
        </div>
      ),
    },
  ];

  return (
    <div className='flex flex-col gap-6'>
      <div>
        <Heading variant='hero' as='h1'>
          Settings
        </Heading>
        <Text variant='body' className='mt-1 text-text-secondary'>
          Preferences, alerts, and account administration
        </Text>
      </div>

      <Tabs
        tabs={tabs}
        defaultTab={initialTabRef.current}
        variant='underline'
        onChange={handleTabChange}
      />
    </div>
  );
}
