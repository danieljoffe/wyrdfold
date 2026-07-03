'use client';

import { useEffect, useState } from 'react';
import { CreditCard } from 'lucide-react';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@danieljoffe/shared-ui/Card';
import { Skeleton } from '@danieljoffe/shared-ui/Skeleton';
import { Text } from '@danieljoffe/shared-ui/Text';
import Button from '@/components/Button';
import { extractApiError } from '@/lib/extractApiError';
import { navigateTo } from '@/lib/navigate';
import { useToast } from '@/state/Toast/ToastProvider';

// Phase 3 tiers (pricing locked 2026-07-03). Copy only — the backend maps
// plans to Stripe Prices; a drifted label here can't change what's billed.
const PLAN_LABELS: Record<string, string> = {
  free: 'Free (bring your own key)',
  starter: 'Starter — $7/mo',
  pro: 'Pro — $19/mo',
};

interface BillingAccount {
  plan: string;
  has_billing_account: boolean;
  byok: boolean;
}

export default function BillingCard() {
  const { toast } = useToast();
  const [loading, setLoading] = useState(true);
  const [account, setAccount] = useState<BillingAccount | null>(null);
  const [redirecting, setRedirecting] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const res = await fetch('/api/billing/account');
        // 404 = billing not offered on this instance (self-host / not
        // configured) — the card renders nothing rather than an error.
        if (!res.ok) return;
        const data = (await res.json()) as BillingAccount;
        if (!cancelled) setAccount(data);
      } catch {
        // Network hiccup: stay hidden; Settings must not block on billing.
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, []);

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
