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
import { Select } from '@danieljoffe/shared-ui/Select';
import { Switch } from '@danieljoffe/shared-ui/Switch';
import Button from '@/components/Button';
import { extractApiError } from '@/lib/extractApiError';
import { useToast } from '@/state/Toast/ToastProvider';

// Mirrors the API's SeniorityLevel ladder (app/models/targets.py), low→high.
export const SENIORITY_LEVELS = [
  'ic',
  'senior',
  'staff',
  'manager',
  'director',
  'vp',
  'c_level',
] as const;
export type SeniorityLevel = (typeof SENIORITY_LEVELS)[number];

const SENIORITY_LABEL: Record<SeniorityLevel, string> = {
  ic: 'IC',
  senior: 'Senior',
  staff: 'Staff',
  manager: 'Manager',
  director: 'Director',
  vp: 'VP',
  c_level: 'C-level',
};

const SENIORITY_OPTIONS = [
  { value: '', label: 'Any' },
  ...SENIORITY_LEVELS.map(l => ({ value: l, label: SENIORITY_LABEL[l] })),
];

/** Per-user, per-target read-time preferences (#60). Mirrors the API model. */
export interface TargetPreferences {
  pref_score_cutoff: number;
  pref_locations: string[] | null;
  pref_remote_ok: boolean;
  pref_seniority_min: SeniorityLevel | null;
  pref_seniority_max: SeniorityLevel | null;
  pref_employment_types: string[] | null;
  pref_include_unknown_salary: boolean;
}

export const DEFAULT_PREFERENCES: TargetPreferences = {
  pref_score_cutoff: 40,
  pref_locations: null,
  pref_remote_ok: true,
  pref_seniority_min: null,
  pref_seniority_max: null,
  pref_employment_types: null,
  pref_include_unknown_salary: true,
};

export const SCORE_MIN = 0;
export const SCORE_MAX = 200;

/** Comma/newline-separated text → trimmed string[] (empty → null). */
export function parseList(raw: string): string[] | null {
  const items = raw
    .split(/[,\n]/)
    .map(s => s.trim())
    .filter(Boolean);
  return items.length ? items : null;
}

export function listToInput(value: string[] | null): string {
  return value ? value.join(', ') : '';
}

/** Parse the score-cutoff input. Blank → the default (40); out-of-range → invalid. */
export function parseScoreCutoff(raw: string): {
  value: number;
  valid: boolean;
} {
  const trimmed = raw.trim();
  if (trimmed === '')
    return { value: DEFAULT_PREFERENCES.pref_score_cutoff, valid: true };
  const n = Number(trimmed);
  if (!Number.isInteger(n) || n < SCORE_MIN || n > SCORE_MAX) {
    return { value: DEFAULT_PREFERENCES.pref_score_cutoff, valid: false };
  }
  return { value: n, valid: true };
}

function toSeniority(raw: string): SeniorityLevel | null {
  return raw === '' ? null : (raw as SeniorityLevel);
}

interface TargetPreferencesEditorProps {
  targetId: string;
}

/**
 * Per-(user, target) read-time preferences (#60): a filter/re-rank over the
 * SHARED cached fit score — never a per-user re-grade. Self-contained: loads
 * the current preferences, edits a full set, and PUTs a full replace (the API
 * resets omitted fields to defaults, so the form always submits every field).
 *
 * Seniority / employment-type filters stay inert until the job-side firewall
 * tags land (the read path treats an untagged job as "keep"), so they're
 * labelled as upcoming; score cutoff + locations + remote are live today.
 */
export default function TargetPreferencesEditor({
  targetId,
}: TargetPreferencesEditorProps) {
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [saved, setSaved] = useState<TargetPreferences>(DEFAULT_PREFERENCES);
  const [saving, setSaving] = useState(false);
  const { toast } = useToast();

  const [cutoffRaw, setCutoffRaw] = useState('40');
  const [locationsRaw, setLocationsRaw] = useState('');
  const [remoteOk, setRemoteOk] = useState(true);
  const [seniorityMin, setSeniorityMin] = useState<SeniorityLevel | null>(null);
  const [seniorityMax, setSeniorityMax] = useState<SeniorityLevel | null>(null);
  const [employmentRaw, setEmploymentRaw] = useState('');
  const [includeUnknownSalary, setIncludeUnknownSalary] = useState(true);

  const hydrate = useCallback((p: TargetPreferences) => {
    setSaved(p);
    setCutoffRaw(String(p.pref_score_cutoff));
    setLocationsRaw(listToInput(p.pref_locations));
    setRemoteOk(p.pref_remote_ok);
    setSeniorityMin(p.pref_seniority_min);
    setSeniorityMax(p.pref_seniority_max);
    setEmploymentRaw(listToInput(p.pref_employment_types));
    setIncludeUnknownSalary(p.pref_include_unknown_salary);
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`/api/targets/${targetId}/preferences`);
        if (!res.ok) throw new Error(String(res.status));
        const data = (await res.json()) as TargetPreferences;
        if (!cancelled) hydrate(data);
      } catch {
        // Fall back to defaults so the form is still usable on a transient
        // error; a real "not linked" 404 surfaces its own toast on save.
        if (!cancelled) {
          setLoadError(true);
          hydrate(DEFAULT_PREFERENCES);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [targetId, hydrate]);

  const cutoff = useMemo(() => parseScoreCutoff(cutoffRaw), [cutoffRaw]);
  const seniorityInverted =
    seniorityMin !== null &&
    seniorityMax !== null &&
    SENIORITY_LEVELS.indexOf(seniorityMin) >
      SENIORITY_LEVELS.indexOf(seniorityMax);
  const hasError = !cutoff.valid || seniorityInverted;

  const draft: TargetPreferences = useMemo(
    () => ({
      pref_score_cutoff: cutoff.value,
      pref_locations: parseList(locationsRaw),
      pref_remote_ok: remoteOk,
      pref_seniority_min: seniorityMin,
      pref_seniority_max: seniorityMax,
      pref_employment_types: parseList(employmentRaw),
      pref_include_unknown_salary: includeUnknownSalary,
    }),
    [
      cutoff.value,
      locationsRaw,
      remoteOk,
      seniorityMin,
      seniorityMax,
      employmentRaw,
      includeUnknownSalary,
    ]
  );

  const isDirty = useMemo(
    () => JSON.stringify(draft) !== JSON.stringify(saved),
    [draft, saved]
  );
  const isCustom = useMemo(
    () => JSON.stringify(saved) !== JSON.stringify(DEFAULT_PREFERENCES),
    [saved]
  );

  const handleSave = useCallback(async () => {
    if (hasError) return;
    setSaving(true);
    try {
      const res = await fetch(`/api/targets/${targetId}/preferences`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(draft),
      });
      if (!res.ok)
        throw new Error(
          await extractApiError(res, 'Failed to save preferences')
        );
      const updated = (await res.json()) as TargetPreferences;
      hydrate(updated);
      toast({ variant: 'success', title: 'Preferences saved' });
    } catch (err) {
      toast({
        variant: 'error',
        title:
          err instanceof Error ? err.message : 'Failed to save preferences',
      });
    } finally {
      setSaving(false);
    }
  }, [hasError, targetId, draft, hydrate, toast]);

  // Populate the form with defaults (the user reviews, then Saves to persist).
  const handleReset = useCallback(() => {
    hydrate(DEFAULT_PREFERENCES);
  }, [hydrate]);

  if (loading) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Preferences</CardTitle>
        </CardHeader>
        <CardContent>
          <div className='flex items-center gap-2 text-text-secondary'>
            <Spinner size='sm' />
            <Text variant='meta'>Loading preferences…</Text>
          </div>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <div className='flex items-baseline justify-between gap-2'>
          <CardTitle>Preferences</CardTitle>
          {isCustom ? (
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
          Filter this target&apos;s jobs list to your taste. These shape only
          your view of the shared list — they never change scoring.
        </Text>
      </CardHeader>

      <CardContent className='flex flex-col gap-5'>
        {loadError && (
          <Text variant='meta' className='text-text-tertiary'>
            Couldn&apos;t load your saved preferences — showing defaults.
          </Text>
        )}

        <div className='max-w-xs'>
          <Input
            label='Minimum fit score'
            type='number'
            inputMode='numeric'
            value={cutoffRaw}
            onChange={e => setCutoffRaw(e.target.value)}
            min={SCORE_MIN}
            max={SCORE_MAX}
            aria-label='Minimum fit score'
            disabled={saving}
            error={!cutoff.valid ? 'Enter a whole number 0–200' : undefined}
            helperText='Hide jobs scoring below this (default 40).'
          />
        </div>

        <Input
          label='Locations'
          value={locationsRaw}
          onChange={e => setLocationsRaw(e.target.value)}
          placeholder='e.g. New York, Remote, Austin'
          aria-label='Locations'
          disabled={saving}
          helperText='Comma-separated. Blank = no location filter.'
        />

        <Switch
          checked={remoteOk}
          onChange={setRemoteOk}
          label='Allow remote roles (pass the location filter)'
          disabled={saving}
        />

        <div className='grid gap-4 sm:grid-cols-2'>
          <Select
            label='Min seniority'
            value={seniorityMin ?? ''}
            onChange={e => setSeniorityMin(toSeniority(e.target.value))}
            options={SENIORITY_OPTIONS}
            disabled={saving}
          />
          <Select
            label='Max seniority'
            value={seniorityMax ?? ''}
            onChange={e => setSeniorityMax(toSeniority(e.target.value))}
            options={SENIORITY_OPTIONS}
            disabled={saving}
          />
        </div>
        {seniorityInverted && (
          <Text variant='meta' className='text-error'>
            Min seniority must not rank above max seniority.
          </Text>
        )}

        <Input
          label='Employment types'
          value={employmentRaw}
          onChange={e => setEmploymentRaw(e.target.value)}
          placeholder='e.g. full_time, contract'
          aria-label='Employment types'
          disabled={saving}
          helperText='Comma-separated. Blank = no employment-type filter.'
        />

        <Switch
          checked={includeUnknownSalary}
          onChange={setIncludeUnknownSalary}
          label='Include jobs with unknown salary'
          disabled={saving}
        />

        <Text variant='meta' className='text-text-tertiary'>
          Seniority and employment-type filters apply once job tagging is live.
        </Text>

        <div className='flex flex-wrap items-center justify-end gap-2'>
          <Button
            name='target-preferences-reset'
            variant='ghost'
            size='sm'
            onClick={handleReset}
            disabled={saving || !isCustom}
          >
            <RotateCcw className='size-4' aria-hidden />
            <span>Reset to defaults</span>
          </Button>
          <Button
            name='target-preferences-save'
            variant='primary'
            size='sm'
            onClick={handleSave}
            disabled={!isDirty || hasError || saving}
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
