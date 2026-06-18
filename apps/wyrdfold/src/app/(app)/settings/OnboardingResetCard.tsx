'use client';

import { useCallback, useState } from 'react';
import { useRouter } from 'next/navigation';
import { RotateCcw } from 'lucide-react';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@danieljoffe/shared-ui/Card';
import { Text } from '@danieljoffe/shared-ui/Text';
import Button from '@/components/Button';
import ConfirmModal from '@/components/ConfirmModal';
import { useToast } from '@/state/Toast/ToastProvider';

/**
 * Settings card that lets a user redo the onboarding wizard.
 *
 * Clears `onboarding_completed_at` + `onboarding_current_step`
 * server-side via POST /api/profile/onboarding/reset, then redirects
 * to /onboarding. **Does not** delete prose, targets, or any other
 * profile data — only the wizard's notion of completion is reset.
 *
 * A styled `ConfirmModal` guards against accidental clicks so the prompt
 * matches the app's design system and can show the reset in flight.
 */
export default function OnboardingResetCard() {
  const router = useRouter();
  const { toast } = useToast();
  const [submitting, setSubmitting] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);

  const handleRedo = useCallback(async () => {
    setSubmitting(true);
    try {
      const res = await fetch('/api/profile/onboarding/reset', {
        method: 'POST',
      });
      if (!res.ok) {
        throw new Error(`Reset failed (${res.status})`);
      }
      setConfirmOpen(false);
      router.push('/onboarding');
    } catch (err) {
      setSubmitting(false);
      toast({
        variant: 'error',
        title: err instanceof Error ? err.message : 'Could not redo onboarding',
      });
    }
  }, [router, toast]);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Redo onboarding</CardTitle>
        <Text variant='meta' className='text-text-secondary'>
          Restart the welcome wizard. Your profile, targets, and saved jobs are
          preserved.
        </Text>
      </CardHeader>
      <CardContent>
        <Button
          name='settings-redo-onboarding'
          variant='outline'
          size='sm'
          onClick={() => setConfirmOpen(true)}
          disabled={submitting}
        >
          <RotateCcw className='size-4' aria-hidden />
          <span>{submitting ? 'Resetting…' : 'Redo onboarding'}</span>
        </Button>
      </CardContent>

      <ConfirmModal
        isOpen={confirmOpen}
        onClose={() => setConfirmOpen(false)}
        onConfirm={handleRedo}
        title='Redo onboarding?'
        message='Your profile data, targets, and saved jobs are preserved — only the wizard restarts.'
        confirmLabel='Redo onboarding'
        loading={submitting}
        loadingLabel='Resetting…'
      />
    </Card>
  );
}
