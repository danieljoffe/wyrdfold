'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import { RotateCcw } from 'lucide-react';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@danieljoffe/shared-ui/Card';
import { Text } from '@danieljoffe/shared-ui/Text';
import { Badge } from '@danieljoffe/shared-ui/Badge';
import { Spinner } from '@danieljoffe/shared-ui/Spinner';
import { Input } from '@danieljoffe/shared-ui/Input';
import Button from '@/components/Button';
import { extractApiError } from '@/lib/extractApiError';
import { useToast } from '@/state/Toast/ToastProvider';
import type { UserTarget } from '../types';

export const THRESHOLD_MIN = 0;
export const THRESHOLD_MAX = 100;

interface NotificationThresholdsEditorProps {
  targetId: string;
  userTarget: UserTarget;
  onUpdated: (next: UserTarget) => void;
}

interface AccountDefaults {
  job: number | null;
  sms: number | null;
}

/** Canonical input string for a nullable threshold ('' === inherit default). */
function toInput(value: number | null): string {
  return value === null ? '' : String(value);
}

/**
 * Parse a threshold input. Blank → null (inherit the account default).
 * A non-integer or out-of-range value is invalid and blocks save.
 */
export function parseThresholdInput(raw: string): {
  value: number | null;
  valid: boolean;
} {
  const trimmed = raw.trim();
  if (trimmed === '') return { value: null, valid: true };
  const n = Number(trimmed);
  if (!Number.isInteger(n) || n < THRESHOLD_MIN || n > THRESHOLD_MAX) {
    return { value: null, valid: false };
  }
  return { value: n, valid: true };
}

/**
 * Per-(user, target) email/SMS alert thresholds (#15).
 *
 * Each channel either overrides the account-wide default (a number) or
 * inherits it (blank → NULL on the DB column; `notify.py` falls back
 * target → profile). The form owns both channels and PATCHes them
 * together; the backend treats an explicit null as "reset to default".
 */
export default function NotificationThresholdsEditor({
  targetId,
  userTarget,
  onUpdated,
}: NotificationThresholdsEditorProps) {
  const [emailDraft, setEmailDraft] = useState(
    toInput(userTarget.job_score_threshold)
  );
  const [smsDraft, setSmsDraft] = useState(
    toInput(userTarget.sms_score_threshold)
  );
  const [saving, setSaving] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [defaults, setDefaults] = useState<AccountDefaults | null>(null);
  const { toast } = useToast();

  // The account-wide thresholds power the "blank = your default (N)" hint.
  // Non-fatal: the editor still works without them (no inherited-value
  // hint), mirroring how the page degrades when fetchUserTarget fails.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch('/api/profile/notifications');
        if (!res.ok) return;
        const data = (await res.json()) as {
          job_score_threshold: number | null;
          sms_score_threshold: number | null;
        };
        if (!cancelled) {
          setDefaults({
            job: data.job_score_threshold,
            sms: data.sms_score_threshold,
          });
        }
      } catch {
        // ignore — the inherited-value hint is a progressive enhancement
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const email = useMemo(() => parseThresholdInput(emailDraft), [emailDraft]);
  const sms = useMemo(() => parseThresholdInput(smsDraft), [smsDraft]);

  const savedEmail = toInput(userTarget.job_score_threshold);
  const savedSms = toInput(userTarget.sms_score_threshold);
  const isDirty =
    emailDraft.trim() !== savedEmail || smsDraft.trim() !== savedSms;
  const hasError = !email.valid || !sms.valid;
  const anyCustom =
    userTarget.job_score_threshold !== null ||
    userTarget.sms_score_threshold !== null;
  const anyBusy = saving || resetting;

  const applyUpdated = useCallback(
    (updated: UserTarget) => {
      onUpdated(updated);
      setEmailDraft(toInput(updated.job_score_threshold));
      setSmsDraft(toInput(updated.sms_score_threshold));
    },
    [onUpdated]
  );

  const patch = useCallback(
    async (body: {
      job_score_threshold: number | null;
      sms_score_threshold: number | null;
    }): Promise<UserTarget> => {
      const res = await fetch(
        `/api/targets/${targetId}/notification-thresholds`,
        {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        }
      );
      if (!res.ok)
        throw new Error(
          await extractApiError(res, 'Failed to save thresholds')
        );
      return (await res.json()) as UserTarget;
    },
    [targetId]
  );

  const handleSave = useCallback(async () => {
    if (hasError) return;
    setSaving(true);
    try {
      const updated = await patch({
        job_score_threshold: email.value,
        sms_score_threshold: sms.value,
      });
      applyUpdated(updated);
      toast({ variant: 'success', title: 'Notification thresholds saved' });
    } catch (err) {
      toast({
        variant: 'error',
        title: err instanceof Error ? err.message : 'Failed to save thresholds',
      });
    } finally {
      setSaving(false);
    }
  }, [hasError, patch, email.value, sms.value, applyUpdated, toast]);

  const handleReset = useCallback(async () => {
    setResetting(true);
    try {
      const updated = await patch({
        job_score_threshold: null,
        sms_score_threshold: null,
      });
      applyUpdated(updated);
      toast({ variant: 'success', title: 'Reset to account defaults' });
    } catch (err) {
      toast({
        variant: 'error',
        title:
          err instanceof Error ? err.message : 'Failed to reset thresholds',
      });
    } finally {
      setResetting(false);
    }
  }, [patch, applyUpdated, toast]);

  const channels = [
    {
      key: 'email',
      label: 'Email alerts',
      inputId: 'notif-threshold-email',
      draft: emailDraft,
      setDraft: setEmailDraft,
      invalid: !email.valid,
      saved: userTarget.job_score_threshold,
      accountDefault: defaults?.job ?? null,
    },
    {
      key: 'sms',
      label: 'SMS alerts',
      inputId: 'notif-threshold-sms',
      draft: smsDraft,
      setDraft: setSmsDraft,
      invalid: !sms.valid,
      saved: userTarget.sms_score_threshold,
      accountDefault: defaults?.sms ?? null,
    },
  ];

  return (
    <Card>
      <CardHeader>
        <div className='flex items-baseline justify-between gap-2'>
          <CardTitle>Notification thresholds</CardTitle>
          {anyCustom ? (
            <Badge variant='info' size='sm'>
              Custom
            </Badge>
          ) : (
            <Badge variant='default' size='sm'>
              Defaults
            </Badge>
          )}
        </div>
        <Text variant='meta' className='text-text-secondary'>
          The minimum match score that alerts you on each channel for this
          target. Leave a field blank to use your account-wide default from
          Settings.
        </Text>
      </CardHeader>

      <CardContent className='flex flex-col gap-5'>
        <div className='flex flex-col gap-4'>
          {channels.map(ch => (
            <div key={ch.key} className='flex flex-col gap-1'>
              <div className='flex items-center justify-between gap-2'>
                <span className='text-sm font-medium text-text-primary'>
                  {ch.label}
                </span>
                {ch.saved === null ? (
                  <Badge variant='default' size='sm'>
                    Account default
                  </Badge>
                ) : (
                  <Badge variant='info' size='sm'>
                    Custom
                  </Badge>
                )}
              </div>
              <div className='max-w-xs'>
                <Input
                  id={ch.inputId}
                  type='number'
                  inputMode='numeric'
                  value={ch.draft}
                  onChange={e => ch.setDraft(e.target.value)}
                  min={THRESHOLD_MIN}
                  max={THRESHOLD_MAX}
                  placeholder={
                    ch.accountDefault !== null
                      ? `Default: ${ch.accountDefault}`
                      : 'Account default'
                  }
                  aria-label={`${ch.label} score threshold`}
                  disabled={anyBusy}
                  error={ch.invalid ? 'Enter a whole number 0–100' : undefined}
                  helperText={
                    ch.accountDefault !== null
                      ? `Blank = your account default (${ch.accountDefault})`
                      : 'Blank = your account default'
                  }
                />
              </div>
            </div>
          ))}
        </div>

        <div className='flex flex-wrap items-center justify-end gap-2'>
          <Button
            name='notification-thresholds-reset'
            variant='ghost'
            size='sm'
            onClick={handleReset}
            disabled={anyBusy || !anyCustom}
          >
            {resetting ? (
              <>
                <Spinner size='sm' />
                <span>Resetting…</span>
              </>
            ) : (
              <>
                <RotateCcw className='size-4' aria-hidden />
                <span>Use account defaults</span>
              </>
            )}
          </Button>
          <Button
            name='notification-thresholds-save'
            variant='primary'
            size='sm'
            onClick={handleSave}
            disabled={!isDirty || hasError || anyBusy}
          >
            {saving ? (
              <>
                <Spinner size='sm' />
                <span>Saving…</span>
              </>
            ) : (
              'Save'
            )}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
