import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import BillingCard from '../BillingCard';

const mockToast = jest.fn();
const mockNavigateTo = jest.fn();

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
    global.fetch = jest.fn() as jest.Mock;
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('renders nothing when billing is not offered (self-host 404)', async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce(
      jsonError(404, 'Not found')
    );
    const { container } = render(<BillingCard />);
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });

  it('shows upgrade buttons for a free-plan user without a billing account', async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce(jsonOk(account()));
    render(<BillingCard />);

    expect(
      await screen.findByText(/Free \(bring your own key\)/)
    ).toBeInTheDocument();
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
