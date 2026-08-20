'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'next/navigation';
import { Alert } from '@danieljoffe/shared-ui/Alert';
import { Card } from '@danieljoffe/shared-ui/Card';
import { Heading } from '@danieljoffe/shared-ui/Heading';
import { Text } from '@danieljoffe/shared-ui/Text';
import Button from '@/components/kit/Button';
import { extractApiError } from '@/lib/extractApiError';
import { navigateTo } from '@/lib/navigate';

interface SubscribeStepProps {
  onComplete: () => void;
}

interface BillingAccount {
  plan: string;
  has_billing_account: boolean;
  byok: boolean;
}

/** Plans that pay for hosted AI. Anything else cannot run the next step. */
const ENTITLED_PLANS = new Set(['starter', 'pro']);

// The plan flip rides the Stripe WEBHOOK, not the browser redirect, so the two
// race. Bounded wait, then we stop claiming — see the comment at the call site
// for why we advance rather than keep waiting.
const WEBHOOK_POLL_ATTEMPTS = 8;
const WEBHOOK_POLL_INTERVAL_MS = 1000;

// Copy only. The backend maps a plan to a Stripe Price and to the real target
// cap (``starter_max_active_targets`` / ``pro_max_active_targets``); a drifted
// label here cannot change what is billed or what is allowed.
const PLANS = [
  {
    id: 'starter' as const,
    name: 'Starter',
    price: '$7/mo',
    detail: 'Two active targets, matched and scored continuously.',
  },
  {
    id: 'pro' as const,
    name: 'Pro',
    price: '$19/mo',
    detail: 'Five active targets — track several roles at once.',
  },
];

function isEntitled(account: BillingAccount): boolean {
  // BYOK counts. Those users pay their own provider directly, so a
  // subscription buys them nothing this step is able to sell them.
  return account.byok || ENTITLED_PLANS.has(account.plan);
}

/**
 * The subscribe gate (#887).
 *
 * WyrdFold is subscribe-to-use: a new account is seeded with an
 * already-elapsed trial stamp, so every AI call 402s from the first second.
 * Each onboarding path's third step is the first one that needs AI, which
 * meant the refusal landed *after* the user had chosen a path, typed their
 * name, and uploaded a resume. This step moves the ask in front of the work.
 *
 * Two properties matter more than the layout:
 *
 * 1. **It fails open.** Anything that leaves entitlement unknown — a 404
 *    because the instance does not sell subscriptions, a network hiccup, a
 *    malformed response — advances the wizard. The real gate is the API's own
 *    402; this step is a guide, and a guide that can seal the wizard shut is
 *    worse than no guide. Worst case the user meets the 402 they would have
 *    met anyway.
 *
 * 2. **It never shows a paywall to someone who has paid.** Returning from
 *    Checkout, we poll briefly for the webhook, then advance regardless.
 */
export default function SubscribeStep({ onComplete }: SubscribeStepProps) {
  const searchParams = useSearchParams();
  const billingReturn = searchParams.get('billing');
  const [loading, setLoading] = useState(true);
  const [redirecting, setRedirecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const wasCancelled = billingReturn === 'cancelled';

  // ``onComplete`` advances the wizard by one step, so firing it twice would
  // skip a step outright. Guard it rather than relying on the effect running
  // once — Strict Mode double-invokes effects in development.
  const advanced = useRef(false);
  const advance = useCallback(() => {
    if (advanced.current) return;
    advanced.current = true;
    onComplete();
  }, [onComplete]);

  useEffect(() => {
    let cancelled = false;

    async function fetchAccount(): Promise<BillingAccount | null> {
      const res = await fetch('/api/billing/account');
      // 404 = this instance does not sell subscriptions (self-host, or Stripe
      // unconfigured). There is nothing to buy, so the gate must not exist.
      if (!res.ok) return null;
      return (await res.json()) as BillingAccount;
    }

    async function load() {
      try {
        const account = await fetchAccount();
        if (cancelled) return;
        if (!account || isEntitled(account)) {
          advance();
          return;
        }

        if (billingReturn === 'success') {
          for (
            let attempt = 0;
            attempt < WEBHOOK_POLL_ATTEMPTS && !cancelled;
            attempt++
          ) {
            await new Promise(r => setTimeout(r, WEBHOOK_POLL_INTERVAL_MS));
            const next = await fetchAccount();
            if (cancelled) return;
            if (!next || isEntitled(next)) {
              advance();
              return;
            }
          }
          // Still not entitled after the window. Advance ANYWAY. They
          // completed Checkout; the failure mode we accept is a later step's
          // 402, which is recoverable and explains itself. The failure mode we
          // refuse is showing a payment form to someone who has just paid.
          if (!cancelled) advance();
        }
      } catch {
        // Entitlement is unknowable from here — fail open. See the note on
        // this component.
        if (!cancelled) advance();
        return;
      } finally {
        // Only leave the loading state if we are actually going to render the
        // gate. Having advanced, this component is about to be unmounted by
        // the wizard — dropping the flag anyway paints a payment form at
        // someone we just decided not to charge, and whether anyone sees it
        // depends on the parent unmounting inside the same commit. That is
        // not a property worth relying on.
        if (!cancelled && !advanced.current) setLoading(false);
      }
    }

    void load();
    return () => {
      cancelled = true;
    };
  }, [billingReturn, advance]);

  const startCheckout = useCallback(async (plan: 'starter' | 'pro') => {
    setRedirecting(true);
    setError(null);
    try {
      const res = await fetch('/api/billing/checkout-session', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        // ``return_to`` keeps Checkout inside the wizard (#887). Without it
        // the API's success_url is hardcoded to /settings, so paying from
        // here dropped the user out of onboarding with no route back.
        body: JSON.stringify({ plan, return_to: 'onboarding' }),
      });
      if (!res.ok) {
        throw new Error(
          await extractApiError(res, 'Checkout is unavailable right now.')
        );
      }
      const { url } = (await res.json()) as { url: string };
      navigateTo(url);
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : 'Checkout is unavailable right now.'
      );
      setRedirecting(false);
    }
  }, []);

  if (loading) {
    return (
      <Card className='p-6'>
        <Text variant='body' className='text-text-secondary'>
          {billingReturn === 'success'
            ? 'Confirming your subscription…'
            : 'Checking your plan…'}
        </Text>
      </Card>
    );
  }

  return (
    <Card className='flex flex-col gap-5 p-6'>
      <div className='flex flex-col gap-2'>
        <Heading variant='section' as='h2'>
          Choose your plan
        </Heading>
        <Text variant='body' className='text-text-secondary'>
          WyrdFold runs on paid AI models, so a subscription covers the rest of
          setup — reading your resume, finding matching roles, and scoring them.
          Cancel any time.
        </Text>
      </div>

      {wasCancelled && (
        <Alert variant='info'>
          Checkout was cancelled and you weren&apos;t charged. Pick a plan when
          you&apos;re ready, or finish setup later.
        </Alert>
      )}

      {error && <Alert variant='error'>{error}</Alert>}

      <div className='flex flex-col gap-3'>
        {PLANS.map(plan => (
          <div
            key={plan.id}
            className='flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border p-4'
          >
            <div className='flex flex-col gap-1'>
              <Text variant='body' className='font-medium'>
                {plan.name} — {plan.price}
              </Text>
              <Text variant='meta' className='text-text-secondary'>
                {plan.detail}
              </Text>
            </div>
            <Button
              name={`onboarding-subscribe-${plan.id}`}
              onClick={() => void startCheckout(plan.id)}
              disabled={redirecting}
            >
              Choose {plan.name}
            </Button>
          </div>
        ))}
      </div>

      <Text variant='caption' className='text-text-tertiary'>
        Payment is handled by Stripe. No card details reach WyrdFold.
      </Text>
    </Card>
  );
}
