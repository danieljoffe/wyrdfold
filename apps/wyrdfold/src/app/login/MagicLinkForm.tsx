'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { Mail, CheckCircle2 } from 'lucide-react';
import { Alert } from '@danieljoffe/shared-ui/Alert';
import { Heading } from '@danieljoffe/shared-ui/Heading';
import { Text } from '@danieljoffe/shared-ui/Text';
import {
  BASE_FIELD,
  FIELD_PADDING,
  FIELD_PLACEHOLDER,
} from '@danieljoffe/shared-ui/styles/formStyles';
import { cn } from '@/lib/cn';
import Button from '@/components/Button';
import WyrdfoldLogo from '@/components/WyrdfoldLogo';
import { createAuthBrowserClient } from '@/lib/supabase/auth-client';

type FormState = 'idle' | 'loading' | 'sent' | 'error';

interface MagicLinkFormProps {
  next: string | undefined;
  authError: string | undefined;
}

const NEXT_COOKIE = 'wyrdfold_login_next';
const NEXT_COOKIE_MAX_AGE_S = 600;
const RESEND_COOLDOWN_S = 30;

/**
 * Stash `next` in a short-lived cookie instead of appending it to
 * `emailRedirectTo`. Adding a query string to the redirect URL forces
 * Supabase to treat it as a different URL and — depending on the
 * project's Redirect URL allowlist — silently fall back to the Site URL
 * (production), dropping `next` and breaking dev login. The cookie sits
 * alongside the request, the callback reads it after exchanging the
 * code, then clears it.
 *
 * `secure` is set when the page is served over HTTPS so the cookie
 * isn't sent in cleartext on any non-HSTS preview/dev host. Skipped
 * on plain http://localhost in dev.
 */
function cookieSecureSuffix(): string {
  return typeof window !== 'undefined' && window.location.protocol === 'https:'
    ? '; secure'
    : '';
}

function stashNextInCookie(next: string): void {
  document.cookie = `${NEXT_COOKIE}=${encodeURIComponent(next)}; max-age=${NEXT_COOKIE_MAX_AGE_S}; path=/; samesite=lax${cookieSecureSuffix()}`;
}

/**
 * Expire the stashed-next cookie immediately. Used so a *failed* sign-in — or
 * a success with no `next` — can't leave a stale redirect target behind that a
 * later successful login would silently honor (frontend audit 2026-07-01, LOW).
 */
function clearNextCookie(): void {
  document.cookie = `${NEXT_COOKIE}=; max-age=0; path=/; samesite=lax${cookieSecureSuffix()}`;
}

// Wyrdfold is invite-only during the beta. The login form sends
// `shouldCreateUser: false`, so non-invited emails get rejected by GoTrue
// (or by the before-user-created hook) with one of these messages. The
// hook now mirrors GoTrue's "User not found" string verbatim so the two
// paths are indistinguishable on the wire (anti-enumeration). Translate
// to copy that hints at the invite gate without naming the gate.
function friendlyAuthError(message: string): string {
  const lower = message.toLowerCase();
  if (
    lower.includes('signups not allowed') ||
    lower.includes('user not found')
  ) {
    return "This email isn't on the beta list yet. If you think it should be, reply to your invitation.";
  }
  return message;
}

// `?auth_error=` codes set by /auth/callback when the magic link
// flow fails. Keep the user-facing copy short — the goal is just to
// stop the link looking like it did nothing.
function callbackErrorCopy(code: string): string {
  switch (code) {
    case 'missing_code':
      // Most common cause: the project's redirect-URL allowlist
      // doesn't include this app's /auth/callback, so Supabase
      // silently substituted Site URL for the redirect and stripped
      // the `?code=...` we needed to exchange.
      return 'Magic link redirect was blocked by Supabase. Check the project’s redirect URL allowlist includes this site’s /auth/callback.';
    case 'flow_state_not_found':
    case 'pkce_verifier_not_found':
      // The PKCE code_verifier cookie was missing. Happens when the
      // email is opened in a different browser/profile from the one
      // that requested the link.
      return 'Open the magic link in the same browser you requested it from, or request a new one.';
    case 'otp_expired':
      return 'That magic link expired. Request a fresh one below.';
    default:
      return `Sign-in failed (${code}). Try requesting a new link.`;
  }
}

export default function MagicLinkForm({ next, authError }: MagicLinkFormProps) {
  const [email, setEmail] = useState('');
  // Surface a callback failure as the initial error state so the user
  // sees *why* the magic link didn't sign them in.
  const [formState, setFormState] = useState<FormState>(
    authError ? 'error' : 'idle'
  );
  const [error, setError] = useState(
    authError ? callbackErrorCopy(authError) : ''
  );
  const [resendIn, setResendIn] = useState(0);
  // Phase 3 slice 5: whether public signup is open (the operator switch).
  // Fail-safe false — closed presentation unless the probe says otherwise.
  const [signupOpen, setSignupOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const res = await fetch('/api/signup-mode');
        if (!res.ok) return;
        const data = (await res.json()) as { mode?: string };
        if (!cancelled && data.mode === 'open') setSignupOpen(true);
      } catch {
        // Probe failure = closed presentation; sign-in still works.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Resend cooldown — counts down from RESEND_COOLDOWN_S after the link is sent.
  useEffect(() => {
    if (formState !== 'sent' || resendIn === 0) return;
    const t = setTimeout(() => setResendIn(s => s - 1), 1000);
    return () => clearTimeout(t);
  }, [formState, resendIn]);

  async function sendLink(e?: React.SyntheticEvent<HTMLFormElement>) {
    e?.preventDefault();
    setFormState('loading');
    setError('');

    const supabase = createAuthBrowserClient();
    const { error: authError } = await supabase.auth.signInWithOtp({
      email,
      options: {
        emailRedirectTo: `${window.location.origin}/auth/callback`,
        // Open signup admits new accounts through the same magic-link
        // flow; when closed, non-existing emails are rejected (and the
        // DB hook backstops any client tampering with this flag).
        shouldCreateUser: signupOpen,
      },
    });

    if (authError) {
      // Sign-in failed — the callback will never run for this attempt, so
      // scrub any stashed destination (including one a prior attempt left)
      // rather than letting it poison the next successful login.
      clearNextCookie();
      setError(friendlyAuthError(authError.message));
      setFormState('error');
    } else {
      // Persist the post-login destination ONLY now that the link is actually
      // on its way. A `next`-less success clears any stale cookie so login
      // lands on the default destination, not a leftover target.
      if (next) {
        stashNextInCookie(next);
      } else {
        clearNextCookie();
      }
      setFormState('sent');
      setResendIn(RESEND_COOLDOWN_S);
    }
  }

  return (
    <main
      id='main-content'
      className='min-h-screen flex flex-col items-center justify-center px-6 py-12'
    >
      <div className='w-full max-w-xs flex flex-col items-center gap-6'>
        <Link
          href='/'
          aria-label='WyrdFold home'
          className='rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2 focus-visible:ring-offset-surface'
        >
          <WyrdfoldLogo aria-hidden className='h-12 w-16 select-none' />
        </Link>

        {formState === 'sent' ? (
          <>
            <div className='flex flex-col items-center gap-2 text-center'>
              <CheckCircle2 className='h-10 w-10 text-brand-500' aria-hidden />
              <Heading variant='hero' as='h1'>
                Check your email
              </Heading>
              <Text variant='body' className='text-text-secondary'>
                Magic link sent to{' '}
                <span className='font-medium text-text-primary'>{email}</span>.
              </Text>
            </div>

            <div className='w-full flex flex-col gap-2'>
              <Button
                name='wyrdfold-resend'
                variant='primary'
                className='w-full'
                disabled={resendIn > 0}
                onClick={() => sendLink()}
              >
                {resendIn > 0 ? `Resend in ${resendIn}s` : 'Resend link'}
              </Button>
              <Button
                name='wyrdfold-back-to-login'
                variant='ghost'
                size='sm'
                className='w-full'
                onClick={() => {
                  setFormState('idle');
                  setEmail('');
                  setResendIn(0);
                }}
              >
                Use a different email
              </Button>
            </div>
          </>
        ) : (
          <>
            <div className='flex flex-col items-center gap-1 text-center'>
              <Heading variant='hero' as='h1'>
                {signupOpen ? 'Sign in or create your account' : 'Sign in'}
              </Heading>
              <Text variant='body' className='text-text-secondary'>
                Two clicks: enter your email, click the link in your inbox.
              </Text>
            </div>

            {!signupOpen && (
              <Alert variant='warning' title='Private beta'>
                WyrdFold is invite-only and under active development. By signing
                in you accept that your account and data may be reset without
                notice while we iterate.
              </Alert>
            )}

            <form onSubmit={sendLink} className='w-full flex flex-col gap-3'>
              <div className='flex flex-col gap-1'>
                <label
                  htmlFor='login-email'
                  className='text-sm font-medium text-text-secondary'
                >
                  Email
                </label>
                <div className='relative'>
                  <Mail
                    className='absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-text-tertiary pointer-events-none'
                    aria-hidden
                  />
                  <input
                    id='login-email'
                    type='email'
                    placeholder='you@example.com'
                    value={email}
                    onChange={e => setEmail(e.target.value)}
                    aria-describedby={
                      formState === 'error' ? 'login-error' : undefined
                    }
                    required
                    data-sentry-mask
                    className={cn(
                      BASE_FIELD,
                      FIELD_PADDING,
                      FIELD_PLACEHOLDER,
                      'pl-9'
                    )}
                  />
                </div>
              </div>
              {formState === 'error' && (
                <Text
                  variant='error'
                  className='text-center'
                  role='alert'
                  id='login-error'
                >
                  {error}
                </Text>
              )}
              <Button
                type='submit'
                name='wyrdfold-sign-in'
                className='w-full'
                disabled={formState === 'loading' || !email}
              >
                {formState === 'loading' ? 'Sending…' : 'Send magic link'}
              </Button>
            </form>
          </>
        )}
      </div>
    </main>
  );
}
