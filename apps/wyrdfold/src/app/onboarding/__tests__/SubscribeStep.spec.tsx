import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import SubscribeStep from '../SubscribeStep';

const mockNavigateTo = jest.fn();
// Mutable so a test can simulate returning from Stripe (?billing=success).
let searchParamsValue = new URLSearchParams();

jest.mock('next/navigation', () => ({
  useSearchParams: () => searchParamsValue,
}));

jest.mock('@/lib/navigate', () => ({
  navigateTo: (url: string) => mockNavigateTo(url),
}));

interface BillingAccount {
  plan: string;
  has_billing_account: boolean;
  byok: boolean;
}

function account(over: Partial<BillingAccount> = {}): BillingAccount {
  return { plan: 'trial', has_billing_account: false, byok: false, ...over };
}

function jsonOk(body: unknown) {
  return { ok: true, status: 200, json: async () => body };
}

function jsonError(status: number, detail: string) {
  // extractApiError reads the body via res.clone().json().
  return {
    ok: false,
    status,
    clone: () => ({ json: async () => ({ detail }) }),
    json: async () => ({ detail }),
  };
}

/** The wizard's `goNext`. Counted, because firing twice skips a step. */
const onComplete = jest.fn();

describe('SubscribeStep', () => {
  beforeEach(() => {
    jest.useFakeTimers({ doNotFake: ['performance'] });
    onComplete.mockClear();
    mockNavigateTo.mockClear();
    searchParamsValue = new URLSearchParams();
    global.fetch = jest.fn() as jest.Mock;
  });

  afterEach(() => {
    jest.runOnlyPendingTimers();
    jest.useRealTimers();
  });

  describe('self-skipping — the gate must never be in the way of someone it has nothing to sell', () => {
    it.each(['starter', 'pro'])('advances a %s subscriber', async plan => {
      (global.fetch as jest.Mock).mockResolvedValue(jsonOk(account({ plan })));

      render(<SubscribeStep onComplete={onComplete} />);

      await waitFor(() => expect(onComplete).toHaveBeenCalledTimes(1));
      // The precondition that makes the assertion meaningful: it advanced
      // without ever putting a payment form on screen.
      expect(screen.queryByText(/choose your plan/i)).not.toBeInTheDocument();
    });

    it('advances a BYOK user, who pays their provider directly', async () => {
      (global.fetch as jest.Mock).mockResolvedValue(
        jsonOk(account({ plan: 'free', byok: true }))
      );

      render(<SubscribeStep onComplete={onComplete} />);

      await waitFor(() => expect(onComplete).toHaveBeenCalledTimes(1));
    });

    it('advances when the instance sells nothing (404 — self-host)', async () => {
      // A self-hosted deployment has no Stripe and no plans. Blocking here
      // would make onboarding impossible to finish rather than merely
      // unguided.
      (global.fetch as jest.Mock).mockResolvedValue(
        jsonError(404, 'Not found')
      );

      render(<SubscribeStep onComplete={onComplete} />);

      await waitFor(() => expect(onComplete).toHaveBeenCalledTimes(1));
    });

    it('advances when entitlement is unknowable (network error)', async () => {
      // Fails OPEN on purpose. The API's own 402 is the real gate; this step
      // is a guide, and a guide that can seal the wizard shut is worse than
      // no guide at all.
      (global.fetch as jest.Mock).mockRejectedValue(new Error('offline'));

      render(<SubscribeStep onComplete={onComplete} />);

      await waitFor(() => expect(onComplete).toHaveBeenCalledTimes(1));
    });
  });

  describe('the gate itself', () => {
    it('offers plans to an unsubscribed user and does NOT advance', async () => {
      (global.fetch as jest.Mock).mockResolvedValue(jsonOk(account()));

      render(<SubscribeStep onComplete={onComplete} />);

      expect(await screen.findByText(/choose your plan/i)).toBeInTheDocument();
      expect(
        screen.getByRole('button', { name: /choose starter/i })
      ).toBeInTheDocument();
      expect(
        screen.getByRole('button', { name: /choose pro/i })
      ).toBeInTheDocument();
      expect(onComplete).not.toHaveBeenCalled();
    });

    it('offers no step-level skip past the gate', async () => {
      // Every other step's "Skip this step" advances to something that still
      // works. Skipping this one lands on a step that 402s — the dead end
      // #887 exists to remove. The wizard's global "Finish setup later" exit
      // lives outside this component and stays available.
      (global.fetch as jest.Mock).mockResolvedValue(jsonOk(account()));

      render(<SubscribeStep onComplete={onComplete} />);
      await screen.findByText(/choose your plan/i);

      expect(
        screen.queryByRole('button', { name: /skip/i })
      ).not.toBeInTheDocument();
    });

    it('sends return_to=onboarding so Checkout comes back here', async () => {
      // The point of the whole change (#887). Without it the API's
      // success_url is hardcoded to /settings, so paying from the wizard
      // dropped the user out of it with no route back.
      const user = userEvent.setup({ advanceTimers: jest.advanceTimersByTime });
      (global.fetch as jest.Mock)
        .mockResolvedValueOnce(jsonOk(account()))
        .mockResolvedValueOnce(
          jsonOk({ url: 'https://checkout.stripe.com/s' })
        );

      render(<SubscribeStep onComplete={onComplete} />);
      await screen.findByText(/choose your plan/i);
      await user.click(screen.getByRole('button', { name: /choose starter/i }));

      await waitFor(() =>
        expect(mockNavigateTo).toHaveBeenCalledWith(
          'https://checkout.stripe.com/s'
        )
      );
      const [url, init] = (global.fetch as jest.Mock).mock.calls[1];
      expect(url).toBe('/api/billing/checkout-session');
      expect(JSON.parse(init.body)).toEqual({
        plan: 'starter',
        return_to: 'onboarding',
      });
    });

    it('surfaces a checkout failure instead of navigating nowhere', async () => {
      const user = userEvent.setup({ advanceTimers: jest.advanceTimersByTime });
      (global.fetch as jest.Mock)
        .mockResolvedValueOnce(jsonOk(account()))
        .mockResolvedValueOnce(jsonError(503, 'Billing is not configured.'));

      render(<SubscribeStep onComplete={onComplete} />);
      await screen.findByText(/choose your plan/i);
      await user.click(screen.getByRole('button', { name: /choose pro/i }));

      expect(
        await screen.findByText(/billing is not configured/i)
      ).toBeInTheDocument();
      expect(mockNavigateTo).not.toHaveBeenCalled();
      // Still retryable — a failed checkout must not disable the buttons.
      expect(screen.getByRole('button', { name: /choose pro/i })).toBeEnabled();
    });
  });

  describe('returning from Stripe', () => {
    it('advances once the webhook lands', async () => {
      searchParamsValue = new URLSearchParams('billing=success');
      (global.fetch as jest.Mock)
        .mockResolvedValueOnce(jsonOk(account()))
        .mockResolvedValue(jsonOk(account({ plan: 'starter' })));

      render(<SubscribeStep onComplete={onComplete} />);
      await screen.findByText(/confirming your subscription/i);

      await jest.advanceTimersByTimeAsync(1500);
      await waitFor(() => expect(onComplete).toHaveBeenCalledTimes(1));
    });

    it('advances anyway when the webhook never lands', async () => {
      // The failure mode we accept is a later step's 402 — recoverable, and
      // it explains itself. The one we refuse is showing a payment form to
      // someone who has just paid.
      searchParamsValue = new URLSearchParams('billing=success');
      (global.fetch as jest.Mock).mockResolvedValue(jsonOk(account()));

      render(<SubscribeStep onComplete={onComplete} />);

      await jest.advanceTimersByTimeAsync(10_000);
      await waitFor(() => expect(onComplete).toHaveBeenCalledTimes(1));
      expect(screen.queryByText(/choose your plan/i)).not.toBeInTheDocument();
    });

    it('says no charge was made after a cancelled checkout, and holds', async () => {
      searchParamsValue = new URLSearchParams('billing=cancelled');
      (global.fetch as jest.Mock).mockResolvedValue(jsonOk(account()));

      render(<SubscribeStep onComplete={onComplete} />);

      expect(await screen.findByText(/weren't charged/i)).toBeInTheDocument();
      // Cancelling is not consent to proceed — the gate stays up.
      expect(onComplete).not.toHaveBeenCalled();
      expect(
        screen.getByRole('button', { name: /choose starter/i })
      ).toBeInTheDocument();
    });
  });
});
