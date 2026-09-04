import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import BillingCard from '../BillingCard';

const mockToast = jest.fn();
const mockNavigateTo = jest.fn();
const mockReplace = jest.fn();
// Mutable so a test can simulate returning from Stripe (?billing=success).
let searchParamsValue = new URLSearchParams();

jest.mock('next/navigation', () => ({
  useSearchParams: () => searchParamsValue,
  usePathname: () => '/settings',
  useRouter: () => ({ replace: mockReplace, prefetch: jest.fn() }),
}));

jest.mock('@/state/Toast/ToastProvider', () => ({
  useToast: () => ({ toast: mockToast }),
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
  return { plan: 'free', has_billing_account: false, byok: false, ...over };
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

describe('BillingCard', () => {
  beforeEach(() => {
    mockToast.mockClear();
    mockNavigateTo.mockClear();
    mockReplace.mockClear();
    searchParamsValue = new URLSearchParams();
    global.fetch = jest.fn() as jest.Mock;
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('confirms the payment on return from Stripe, then strips the param', async () => {
    // #863: Stripe returns to /settings?billing=success and nothing read it,
    // so the highest-anxiety moment in the product answered "did that work?"
    // with an unrelated settings form.
    searchParamsValue = new URLSearchParams('billing=success');
    (global.fetch as jest.Mock).mockResolvedValue(
      jsonOk(account({ plan: 'starter', has_billing_account: true }))
    );

    render(<BillingCard />);

    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          variant: 'success',
          title: expect.stringMatching(/subscription active/i),
        })
      )
    );
    // Stripped, so a refresh doesn't replay the confirmation.
    await waitFor(() =>
      expect(mockReplace).toHaveBeenCalledWith('/settings', { scroll: false })
    );
  });

  it('says no charge was made when checkout is cancelled', async () => {
    searchParamsValue = new URLSearchParams('billing=cancelled');
    (global.fetch as jest.Mock).mockResolvedValue(jsonOk(account()));

    render(<BillingCard />);

    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          description: expect.stringMatching(/no charge was made/i),
        })
      )
    );
  });

  it('waits for the webhook rather than showing a stale plan as final', async () => {
    // The plan flip rides the WEBHOOK, not the redirect, so the two race.
    // Landing first used to show "Free" beside a success message, which
    // reads as a failed payment and invites paying twice.
    searchParamsValue = new URLSearchParams('billing=success');
    (global.fetch as jest.Mock)
      .mockResolvedValueOnce(jsonOk(account({ plan: 'trial' })))
      .mockResolvedValue(
        jsonOk(account({ plan: 'starter', has_billing_account: true }))
      );

    render(<BillingCard />);

    // It must land on the settled plan, not the stale one it first saw.
    expect(
      await screen.findByText(/Starter/, {}, { timeout: 4000 })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /manage subscription/i })
    ).toBeInTheDocument();
  });

  it('says nothing when there is no billing param', async () => {
    (global.fetch as jest.Mock).mockResolvedValue(jsonOk(account()));
    render(<BillingCard />);
    await screen.findByText(/^Free$/);
    expect(mockToast).not.toHaveBeenCalled();
    expect(mockReplace).not.toHaveBeenCalled();
  });

  it('renders nothing when billing is not offered (self-host 404)', async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce(
      jsonError(404, 'Not found')
    );
    const { container } = render(<BillingCard />);
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });

  it('tells a trial user to subscribe, never to add an API key', async () => {
    // A trial runs on HOSTED keys. Showing it the free-plan copy ("add one
    // above") points at a field that is disabled when BYOK is unavailable —
    // the dead end #841 exists to remove.
    //
    // It must not describe a countdown either (#887). `trial` is now simply
    // what an unsubscribed account is stamped with, seeded already-elapsed,
    // so "Free trial" and "Subscribe before it ends" both name a window that
    // never opens. Asserted as absences so the old copy cannot come back.
    (global.fetch as jest.Mock).mockResolvedValueOnce(
      jsonOk(account({ plan: 'trial' }))
    );
    render(<BillingCard />);

    expect(await screen.findByText(/No subscription/)).toBeInTheDocument();
    expect(
      screen.getByText(/AI features need an active subscription/)
    ).toBeInTheDocument();
    expect(screen.queryByText(/Free trial/)).not.toBeInTheDocument();
    expect(screen.queryByText(/before it ends/)).not.toBeInTheDocument();
    expect(screen.queryByText(/add one above/)).not.toBeInTheDocument();
    // Still convertible: the upgrade path must remain on screen.
    expect(
      screen.getByRole('button', { name: /Get Starter/ })
    ).toBeInTheDocument();
  });

  it('shows upgrade buttons for a free-plan user without a billing account', async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce(jsonOk(account()));
    render(<BillingCard />);

    expect(await screen.findByText(/^Free$/)).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /Get Starter/ })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /Upgrade to Pro/ })
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /Manage subscription/ })
    ).not.toBeInTheDocument();
  });

  it('shows manage-subscription for a subscribed user and opens the portal', async () => {
    (global.fetch as jest.Mock)
      .mockResolvedValueOnce(
        jsonOk(account({ plan: 'pro', has_billing_account: true }))
      )
      .mockResolvedValueOnce(jsonOk({ url: 'https://billing.stripe.com/p/1' }));
    render(<BillingCard />);
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole('button', { name: /Manage subscription/ })
    );

    await waitFor(() =>
      expect(mockNavigateTo).toHaveBeenCalledWith(
        'https://billing.stripe.com/p/1'
      )
    );
    expect(global.fetch).toHaveBeenLastCalledWith(
      '/api/billing/portal-session',
      expect.objectContaining({ method: 'POST' })
    );
  });

  it('starts checkout with the chosen plan and redirects to Stripe', async () => {
    (global.fetch as jest.Mock)
      .mockResolvedValueOnce(jsonOk(account()))
      .mockResolvedValueOnce(
        jsonOk({ url: 'https://checkout.stripe.com/c/1' })
      );
    render(<BillingCard />);
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole('button', { name: /Get Starter/ })
    );

    await waitFor(() =>
      expect(mockNavigateTo).toHaveBeenCalledWith(
        'https://checkout.stripe.com/c/1'
      )
    );
    const [url, init] = (global.fetch as jest.Mock).mock.calls.at(-1)!;
    expect(url).toBe('/api/billing/checkout-session');
    expect(JSON.parse(init.body)).toEqual({ plan: 'starter' });
  });

  it('surfaces a checkout failure as a toast and stays on the page', async () => {
    (global.fetch as jest.Mock)
      .mockResolvedValueOnce(jsonOk(account()))
      .mockResolvedValueOnce(
        jsonError(503, 'Billing for the starter plan is not configured.')
      );
    render(<BillingCard />);
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole('button', { name: /Get Starter/ })
    );

    await waitFor(() => expect(mockToast).toHaveBeenCalled());
    expect(mockToast.mock.calls[0][0]).toMatchObject({ variant: 'error' });
    expect(mockNavigateTo).not.toHaveBeenCalled();
  });

  it('explains the BYOK state instead of quota copy', async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce(
      jsonOk(account({ plan: 'pro', byok: true, has_billing_account: true }))
    );
    render(<BillingCard />);

    expect(
      await screen.findByText(/Your own OpenRouter key pays/)
    ).toBeInTheDocument();
  });
});

describe('BillingCard free-plan copy vs server BYOK capability (#858)', () => {
  beforeEach(() => {
    searchParamsValue = new URLSearchParams();
    global.fetch = jest.fn() as jest.Mock;
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('no-BYOK server: stops saying "add one above" and points at plans', async () => {
    // #858: "add one above" pointed at key fields the API-keys card had just
    // said are unavailable — and this screen is what every canceled or
    // payment-failed subscriber lands on (plan="free" on any non-active
    // status).
    (global.fetch as jest.Mock).mockResolvedValue(
      jsonOk({
        plan: 'free',
        has_billing_account: false,
        byok: false,
        byok_available: false,
      })
    );
    render(<BillingCard />);
    expect(
      await screen.findByText(
        /bring-your-own-key isn't offered on this server/i
      )
    ).toBeInTheDocument();
    expect(screen.queryByText(/add one above/i)).not.toBeInTheDocument();
  });

  it('BYOK-capable server keeps the add-a-key path', async () => {
    (global.fetch as jest.Mock).mockResolvedValue(
      jsonOk({
        plan: 'free',
        has_billing_account: false,
        byok: false,
        byok_available: true,
      })
    );
    render(<BillingCard />);
    expect(await screen.findByText(/add one above/i)).toBeInTheDocument();
  });

  it('pre-#858 API payload (no byok_available) keeps the old copy — mixed-deploy window', async () => {
    (global.fetch as jest.Mock).mockResolvedValue(
      jsonOk(account({ plan: 'free' }))
    );
    render(<BillingCard />);
    expect(await screen.findByText(/add one above/i)).toBeInTheDocument();
  });
});
