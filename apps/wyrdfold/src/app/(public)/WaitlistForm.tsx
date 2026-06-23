'use client';

import { useId, useState } from 'react';
import { ArrowRight, CheckCircle2, Mail } from 'lucide-react';
import { Text } from '@danieljoffe/shared-ui/Text';
import {
  BASE_FIELD,
  FIELD_PADDING,
  FIELD_PLACEHOLDER,
} from '@danieljoffe/shared-ui/styles/formStyles';
import { cn } from '@/lib/cn';
import Button from '@/components/Button';

type FormState = 'idle' | 'loading' | 'success' | 'error';

// Mirror of the server-side gate in /api/waitlist so obviously-bad input is
// caught before the round trip. The server is the source of truth — this is
// UX, not security.
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const GENERIC_ERROR = 'Something went wrong. Please try again.';

export default function WaitlistForm() {
  const [email, setEmail] = useState('');
  const [formState, setFormState] = useState<FormState>('idle');
  const [error, setError] = useState('');

  const inputId = useId();
  const errorId = useId();

  async function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();

    const trimmed = email.trim();
    if (!EMAIL_RE.test(trimmed)) {
      setError('Please enter a valid email address.');
      setFormState('error');
      return;
    }

    setFormState('loading');
    setError('');

    try {
      const res = await fetch('/api/waitlist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: trimmed }),
      });

      if (res.ok) {
        setFormState('success');
        return;
      }

      let message = GENERIC_ERROR;
      try {
        const data = (await res.json()) as { error?: unknown };
        if (typeof data.error === 'string' && data.error) message = data.error;
      } catch {
        // Non-JSON error body — keep the generic message.
      }
      setError(message);
      setFormState('error');
    } catch {
      setError(GENERIC_ERROR);
      setFormState('error');
    }
  }

  if (formState === 'success') {
    return (
      <div
        role='status'
        className='flex items-center gap-3 rounded-md border border-success/40 bg-success/10 px-4 py-3'
      >
        <CheckCircle2
          className='size-5 shrink-0 text-success'
          aria-hidden='true'
        />
        <Text variant='body' as='p' className='text-text-primary'>
          You&apos;re on the list. We&apos;ll email you when a spot opens up.
        </Text>
      </div>
    );
  }

  const isLoading = formState === 'loading';

  return (
    <form
      onSubmit={onSubmit}
      noValidate
      className='flex w-full max-w-md flex-col gap-2'
    >
      <label htmlFor={inputId} className='sr-only'>
        Email address
      </label>
      <div className='flex flex-col gap-2 sm:flex-row'>
        <div className='relative flex-1'>
          <Mail
            className='pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-text-tertiary'
            aria-hidden='true'
          />
          <input
            id={inputId}
            type='email'
            name='email'
            inputMode='email'
            autoComplete='email'
            placeholder='you@example.com'
            value={email}
            onChange={e => setEmail(e.target.value)}
            disabled={isLoading}
            required
            aria-invalid={formState === 'error'}
            aria-describedby={formState === 'error' ? errorId : undefined}
            data-sentry-mask
            className={cn(BASE_FIELD, FIELD_PADDING, FIELD_PLACEHOLDER, 'pl-9')}
          />
        </div>
        <Button
          type='submit'
          name='wyrdfold-waitlist-join'
          variant='primary'
          size='md'
          disabled={isLoading}
        >
          {isLoading ? 'Joining…' : 'Join the waitlist'}
          {!isLoading && <ArrowRight className='size-4' aria-hidden='true' />}
        </Button>
      </div>
      {formState === 'error' && (
        <Text variant='error' as='p' role='alert' id={errorId}>
          {error}
        </Text>
      )}
    </form>
  );
}
