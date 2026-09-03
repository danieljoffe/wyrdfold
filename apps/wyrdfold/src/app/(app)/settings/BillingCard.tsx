'use client';

import { useEffect, useRef, useState } from 'react';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { CreditCard } from 'lucide-react';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@danieljoffe/shared-ui/Card';
import { Skeleton } from '@danieljoffe/shared-ui/Skeleton';
import { Text } from '@danieljoffe/shared-ui/Text';
import Button from '@/components/kit/Button';
import { extractApiError } from '@/lib/extractApiError';
import { navigateTo } from '@/lib/navigate';
import { useToast } from '@/state/Toast/ToastProvider';

// Phase 3 tiers (pricing locked 2026-07-03). Copy only — the backend maps
// plans to Stripe Prices; a drifted label here can't change what's billed.
const PLAN_LABELS: Record<string, string> = {
  // Plain "Free" (#858): the old "(bring your own key)" clause contradicted
  // servers where BYOK is not offered — the branched copy below explains
  // what free means on THIS server instead.
  free: 'Free',
  // NOT "Free trial" (#887). `trial` is the stamp an unsubscribed account
  // carries, with an already-elapsed clock — naming it a trial promises a
  // window that never opens.
  trial: 'No subscription',
  starter: 'Starter — $7/mo',
  pro: 'Pro — $19/mo',
};

interface BillingAccount {
  plan: string;
  has_billing_account: boolean;
  byok: boolean;
  /** Whether this SERVER offers BYOK at all (#858) — distinct from `byok`
   *  (this user has a key). Absent on pre-#858 API responses (mixed-deploy
   *  window): treated as available, i.e. the old copy. */
  byok_available?: boolean;
}

export default function BillingCard() {
  const { toast } = useToast();
  const [loading, setLoading] = useState(true);
  const [account, setAccount] = useState<BillingAccount | null>(null);
  const [redirecting, setRedirecting] = useState(false);
  // Stripe returns to /settings?billing=success|cancelled. Nothing read it,
  // so the highest-anxiety moment in the product — money just moved —
  // answered "did that work?" with an unrelated settings form (#863).
  const searchParams = useSearchParams();
  const pathname = usePathname();
  const router = useRouter();
  const billingReturn = searchParams.get('billing');
  // The plan flip rides the WEBHOOK, not this redirect, so the two race.
  // Landing first shows a stale plan; without this the user reads that as a
  // failed payment and may pay twice.
  const [awaitingWebhook, setAwaitingWebhook] = useState(
    billingReturn === 'success'
  );
  const announced = useRef(false);

  useEffect(() => {
    let cancelled = false;

    async function fetchAccount(): Promise<BillingAccount | null> {
      const res = await fetch('/api/billing/account');
      // 404 = billing not offered on this instance (self-host / not
      // configured) — the card renders nothing rather than an error.
      if (!res.ok) return null;
      return (await res.json()) as BillingAccount;
    }

    async function load() {
      try {
        const data = await fetchAccount();
        if (cancelled || !data) return;
        setAccount(data);

        // Poll briefly for the webhook to land, rather than rendering a
        // stale "Free" next to a success message. Bounded: ~8s, then we
        // stop claiming and let the card show whatever is true.
        if (billingReturn === 'success' && !data.has_billing_account) {
          for (let attempt = 0; attempt < 8 && !cancelled; attempt++) {
            await new Promise(r => setTimeout(r, 1000));
            const next = await fetchAccount();
            if (cancelled) return;
            if (next) setAccount(next);
            if (next?.has_billing_account) break;
          }
        }
      } catch {
        // Network hiccup: stay hidden; Settings must not block on billing.
      } finally {
        if (!cancelled) {
          setLoading(false);
          setAwaitingWebhook(false);
        }
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [billingReturn]);

  // Announce the outcome once, then strip the param so a refresh doesn't
  // replay it.
  useEffect(() => {
    if (!billingReturn || announced.current) return;
    announced.current = true;
    if (billingReturn === 'success') {
      toast({
        variant: 'success',
        title: 'Subscription active',
        description: 'Thanks — your plan is updated below.',
      });
    } else if (billingReturn === 'cancelled') {
      toast({
        variant: 'info',
        title: 'Checkout cancelled',
        description: 'No charge was made. Your plan is unchanged.',
      });
    }
    const params = new URLSearchParams(searchParams.toString());
    params.delete('billing');
    const qs = params.toString();
    router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
  }, [billingReturn, searchParams, pathname, router, toast]);

  async function redirectTo(path: string, body?: unknown) {
    setRedirecting(true);
    try {
      const init: RequestInit = { method: 'POST' };
      if (body !== undefined) {
        init.headers = { 'Content-Type': 'application/json' };
        init.body = JSON.stringify(body);
      }
      const res = await fetch(path, init);
      if (!res.ok) {
        throw new Error(
          await extractApiError(res, 'Billing is unavailable right now.')
        );
      }
      const { url } = (await res.json()) as { url: string };
      navigateTo(url);
    } catch (err) {
      toast({
        variant: 'error',
        title:
          err instanceof Error
            ? err.message
            : 'Billing is unavailable right now.',
      });
      setRedirecting(false);
    }
  }

  if (loading) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className='flex items-center gap-2'>
            <CreditCard className='h-4 w-4' aria-hidden /> Plan &amp; billing
          </CardTitle>
        </CardHeader>
        <CardContent>
          <Skeleton className='h-16 w-full' />
        </CardContent>
      </Card>
    );
  }

  if (!account) return null;

  const managed = account.plan === 'starter' || account.plan === 'pro';

  return (
    <Card>
      <CardHeader>
        <CardTitle className='flex items-center gap-2'>
          <CreditCard className='h-4 w-4' aria-hidden /> Plan &amp; billing
        </CardTitle>
      </CardHeader>
      <CardContent className='flex flex-col gap-3'>
        {awaitingWebhook && (
          <Text variant='meta' className='text-text-secondary'>
            Confirming your subscription…
          </Text>
        )}
        <Text variant='body'>
          Current plan:{' '}
          <span className='font-medium'>
            {PLAN_LABELS[account.plan] ?? account.plan}
          </span>
        </Text>
        {account.byok ? (
          <Text variant='meta' className='text-text-secondary'>
            Your own OpenRouter key pays for AI features — no managed quota
            applies.
          </Text>
        ) : managed ? (
          <Text variant='meta' className='text-text-secondary'>
            AI features run on hosted keys with your plan&apos;s monthly
            allowance.
          </Text>
        ) : account.plan === 'trial' ? (
          /* A trial runs on HOSTED keys, so it must NOT get the free-plan
             copy telling the user to add an OpenRouter key — that is the
             dead end #841 exists to remove.

             It must also not say the trial is still running (#887). There is
             no trial period any more — the Terms say so — and `trial` is now
             simply what an unsubscribed account is stamped with, seeded with
             an already-elapsed clock so it gets no free inference. "Subscribe
             before it ends" describes a countdown that finished before the
             user read the sentence. */
          <Text variant='meta' className='text-text-secondary'>
            AI features need an active subscription. Choose a plan below to
            start job matching and tailored resumes.
          </Text>
        ) : account.byok_available === false ? (
          /* #858: on a server with no BYOK_MASTER_KEY the key fields above
             are disabled — "add one above" pointed at a control the API-keys
             card had just said doesn't exist. This is also the screen every
             canceled or payment-failed subscriber lands on (billing.py sets
             plan="free" on any non-active status), so it must tell one
             coherent story. */
          <Text variant='meta' className='text-text-secondary'>
            AI features aren&apos;t included on the free plan, and
            bring-your-own-key isn&apos;t offered on this server — choose a plan
            below to enable job matching and tailored resumes.
          </Text>
        ) : (
          <Text variant='meta' className='text-text-secondary'>
            The free plan uses your own OpenRouter key (add one above), or
            upgrade for hosted AI with a monthly allowance.
          </Text>
        )}
        <div className='flex flex-wrap gap-2'>
          {account.has_billing_account ? (
            <Button
              onClick={() => void redirectTo('/api/billing/portal-session')}
              disabled={redirecting}
            >
              Manage subscription
            </Button>
          ) : (
            <>
              <Button
                onClick={() =>
                  void redirectTo('/api/billing/checkout-session', {
                    plan: 'starter',
                  })
                }
                disabled={redirecting}
              >
                Get Starter — $7/mo
              </Button>
              <Button
                onClick={() =>
                  void redirectTo('/api/billing/checkout-session', {
                    plan: 'pro',
                  })
                }
                disabled={redirecting}
              >
                Upgrade to Pro — $19/mo
              </Button>
            </>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
