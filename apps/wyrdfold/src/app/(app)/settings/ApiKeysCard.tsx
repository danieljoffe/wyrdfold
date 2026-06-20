'use client';

import { useCallback, useEffect, useState } from 'react';
import { KeyRound } from 'lucide-react';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@danieljoffe/shared-ui/Card';
import { Input } from '@danieljoffe/shared-ui/Input';
import { Skeleton } from '@danieljoffe/shared-ui/Skeleton';
import { Text } from '@danieljoffe/shared-ui/Text';
import Button from '@/components/Button';
import ConfirmModal from '@/components/ConfirmModal';
import { extractApiError } from '@/lib/extractApiError';
import { useToast } from '@/state/Toast/ToastProvider';

// v1 is OpenRouter-only (#5). The backend rejects other providers, so the
// card hard-codes the one provider rather than rendering a list.
const PROVIDER = 'openrouter';
const OPENROUTER_KEYS_URL = 'https://openrouter.ai/settings/keys';

interface KeyMeta {
  provider: string;
  last4: string | null;
  created_at: string;
  updated_at: string;
  rotated_at: string | null;
}

interface KeysResponse {
  available: boolean;
  keys: KeyMeta[];
}

export default function ApiKeysCard() {
  const { toast } = useToast();
  const [loading, setLoading] = useState(true);
  const [available, setAvailable] = useState(false);
  const [meta, setMeta] = useState<KeyMeta | null>(null);
  const [keyInput, setKeyInput] = useState('');
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [removing, setRemoving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const res = await fetch('/api/profile/keys');
        if (!res.ok) throw new Error(`${res.status}`);
        const data = (await res.json()) as KeysResponse;
        if (cancelled) return;
        setAvailable(data.available);
        setMeta(data.keys.find(k => k.provider === PROVIDER) ?? null);
      } catch {
        // Treat a failed load as "unavailable" — the card collapses to a
        // muted note rather than blocking the rest of Settings.
        if (!cancelled) setAvailable(false);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, []);

  const handleSave = useCallback(async () => {
    const key = keyInput.trim();
    if (!key) return;
    setSaving(true);
    try {
      const res = await fetch(`/api/profile/keys/${PROVIDER}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key }),
      });
      if (!res.ok) {
        throw new Error(await extractApiError(res, 'Could not save your key'));
      }
      const saved = (await res.json()) as KeyMeta;
      setMeta(saved);
      setKeyInput('');
      setEditing(false);
      toast({ variant: 'success', title: 'OpenRouter key saved' });
    } catch (err) {
      toast({
        variant: 'error',
        title: err instanceof Error ? err.message : 'Could not save your key',
      });
    } finally {
      setSaving(false);
    }
  }, [keyInput, toast]);

  const handleRemove = useCallback(async () => {
    setRemoving(true);
    try {
      const res = await fetch(`/api/profile/keys/${PROVIDER}`, {
        method: 'DELETE',
      });
      if (!res.ok) {
        throw new Error(
          await extractApiError(res, 'Could not remove your key')
        );
      }
      setMeta(null);
      setKeyInput('');
      setEditing(false);
      setConfirmOpen(false);
      toast({ variant: 'success', title: 'OpenRouter key removed' });
    } catch (err) {
      toast({
        variant: 'error',
        title: err instanceof Error ? err.message : 'Could not remove your key',
      });
    } finally {
      setRemoving(false);
    }
  }, [toast]);

  const showForm = !meta || editing;

  return (
    <Card>
      <CardHeader>
        <CardTitle className='flex items-center gap-2'>
          <KeyRound className='size-4' aria-hidden />
          OpenRouter API key
        </CardTitle>
        <Text variant='meta' className='text-text-secondary'>
          Bring your own{' '}
          <a
            href={OPENROUTER_KEYS_URL}
            target='_blank'
            rel='noopener noreferrer'
            className='underline'
          >
            OpenRouter key
          </a>{' '}
          so AI features bill your account instead of the shared allowance.
        </Text>
      </CardHeader>
      <CardContent className='flex flex-col gap-4'>
        {loading ? (
          <div className='flex flex-col gap-2' aria-label='Loading key status'>
            <Skeleton width='50%' size='sm' />
            <Skeleton variant='rectangular' height={36} />
          </div>
        ) : !available ? (
          <Text variant='meta' className='text-text-tertiary'>
            Bring-your-own-key isn’t enabled on this instance — it runs on the
            operator’s keys.
          </Text>
        ) : (
          <>
            {meta && !editing && (
              <div className='flex flex-wrap items-center justify-between gap-3'>
                <div className='flex flex-col gap-0.5'>
                  <Text variant='caption' className='font-mono'>
                    •••• {meta.last4 ?? '????'}
                  </Text>
                  <Text variant='meta' className='text-text-tertiary'>
                    {meta.rotated_at
                      ? `Rotated ${new Date(meta.rotated_at).toLocaleDateString()}`
                      : `Added ${new Date(meta.created_at).toLocaleDateString()}`}
                  </Text>
                </div>
                <div className='flex items-center gap-2'>
                  <Button
                    name='settings-rotate-openrouter-key'
                    variant='outline'
                    size='sm'
                    onClick={() => setEditing(true)}
                  >
                    Rotate
                  </Button>
                  <Button
                    name='settings-remove-openrouter-key'
                    variant='error'
                    size='sm'
                    onClick={() => setConfirmOpen(true)}
                  >
                    Remove
                  </Button>
                </div>
              </div>
            )}

            {showForm && (
              <div className='flex flex-col gap-3'>
                <Input
                  label={meta ? 'New OpenRouter key' : 'OpenRouter key'}
                  type='password'
                  value={keyInput}
                  onChange={e => setKeyInput(e.target.value)}
                  placeholder='sk-or-…'
                  helperText='Tip: create a key with a spend limit so a runaway run can’t drain your balance.'
                  autoComplete='off'
                  spellCheck={false}
                  data-sentry-mask
                  disabled={saving}
                />
                <div className='flex items-center gap-2'>
                  <Button
                    name='settings-save-openrouter-key'
                    variant='primary'
                    size='sm'
                    onClick={handleSave}
                    disabled={saving || keyInput.trim().length === 0}
                  >
                    {saving ? 'Saving…' : meta ? 'Save new key' : 'Save key'}
                  </Button>
                  {meta && (
                    <Button
                      name='settings-cancel-openrouter-key'
                      variant='ghost'
                      size='sm'
                      onClick={() => {
                        setEditing(false);
                        setKeyInput('');
                      }}
                      disabled={saving}
                    >
                      Cancel
                    </Button>
                  )}
                </div>
              </div>
            )}
          </>
        )}
      </CardContent>

      <ConfirmModal
        isOpen={confirmOpen}
        onClose={() => setConfirmOpen(false)}
        onConfirm={handleRemove}
        title='Remove OpenRouter key?'
        message='AI features will fall back to the shared allowance (or stop, on instances that require your own key).'
        confirmLabel='Remove key'
        loading={removing}
        loadingLabel='Removing…'
      />
    </Card>
  );
}
