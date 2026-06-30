'use client';

import { useCallback, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Trash2 } from 'lucide-react';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@danieljoffe/shared-ui/Card';
import { Input } from '@danieljoffe/shared-ui/Input';
import { Text } from '@danieljoffe/shared-ui/Text';
import Button from '@/components/Button';
import ConfirmModal from '@/components/ConfirmModal';
import { extractApiError } from '@/lib/extractApiError';
import { useToast } from '@/state/Toast/ToastProvider';

// The user must type this exact phrase (case/space-insensitive) to enable the
// destructive action — a deliberate, hard-to-fat-finger confirmation.
const CONFIRM_PHRASE = 'delete my account';

/**
 * Danger-zone settings card for right-to-erasure (#29/#82). Permanently
 * deletes the account via DELETE /api/profile/account — every per-user row,
 * uploaded files, and the auth user — then signs out and leaves. Gated behind
 * a typed confirmation because it's irreversible. On success there's nothing
 * to return to, so we clear the local session and redirect to /login rather
 * than toasting on a now-deleted account.
 */
export default function DeleteAccountCard() {
  const router = useRouter();
  const { toast } = useToast();
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState('');
  const [deleting, setDeleting] = useState(false);

  const close = useCallback(() => {
    if (deleting) return;
    setOpen(false);
    setTyped('');
  }, [deleting]);

  const handleDelete = useCallback(async () => {
    setDeleting(true);
    try {
      const res = await fetch('/api/profile/account', { method: 'DELETE' });
      if (!res.ok) {
        toast({
          variant: 'error',
          title: await extractApiError(res, 'Account deletion failed'),
        });
        setDeleting(false);
        return;
      }
      // Account (incl. the auth user) is gone server-side. Clear the local
      // session so middleware doesn't try to refresh a dead JWT, then leave.
      // signOut may itself fail (session already invalid) — that's fine, we
      // navigate regardless. Don't reset ``deleting``: we're unmounting.
      try {
        const { createAuthBrowserClient } =
          await import('@/lib/supabase/auth-client');
        await createAuthBrowserClient().auth.signOut();
      } catch {
        // Session already invalid server-side; cookies clear on next request.
      }
      router.replace('/login');
      router.refresh();
    } catch {
      toast({ variant: 'error', title: 'Network error deleting your account' });
      setDeleting(false);
    }
  }, [router, toast]);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Delete account</CardTitle>
        <Text variant='meta' className='text-text-secondary'>
          Permanently delete your account and everything tied to it — profile,
          targets, saved jobs, generated documents, uploaded resumes, and stored
          API keys. This cannot be undone. Consider exporting your data first.
        </Text>
      </CardHeader>
      <CardContent>
        <Button
          name='settings-delete-account'
          variant='error'
          size='sm'
          onClick={() => setOpen(true)}
        >
          <Trash2 className='size-4' aria-hidden />
          <span>Delete my account</span>
        </Button>
      </CardContent>

      <ConfirmModal
        isOpen={open}
        onClose={close}
        onConfirm={handleDelete}
        title='Delete your account?'
        destructive
        loading={deleting}
        loadingLabel='Deleting…'
        confirmLabel='Delete account'
        confirmDisabled={typed.trim().toLowerCase() !== CONFIRM_PHRASE}
        name='delete-account'
        message={
          <div className='space-y-3'>
            <Text as='p' variant='body'>
              This permanently erases your account and all associated data. It
              cannot be undone, and you&apos;ll be signed out immediately.
            </Text>
            <Input
              label={`Type "${CONFIRM_PHRASE}" to confirm`}
              value={typed}
              onChange={e => setTyped(e.target.value)}
              placeholder={CONFIRM_PHRASE}
              autoComplete='off'
              disabled={deleting}
            />
          </div>
        }
      />
    </Card>
  );
}
