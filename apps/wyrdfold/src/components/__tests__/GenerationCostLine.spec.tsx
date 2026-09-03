import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import GenerationCostLine from '../GenerationCostLine';

// #867: what a generation "cost" means depends on who paid. BYOK users see
// their own provider spend (raw cost + model); managed users pay a
// subscription, so the same number is framed as allowance consumption and
// the operator's model name stays out of the tooltip.

const PROPS = {
  costUsd: 0.0043,
  inputTokens: 1200,
  outputTokens: 300,
  model: 'anthropic/claude-sonnet-4.6',
  latencyMs: 2400,
};

const originalFetch = global.fetch;
afterEach(() => {
  global.fetch = originalFetch;
  jest.clearAllMocks();
});

function mockAccount(body: unknown, ok = true) {
  global.fetch = jest.fn().mockResolvedValue({
    ok,
    json: async () => body,
  } as Response) as unknown as typeof fetch;
}

describe('GenerationCostLine payer framing', () => {
  it('managed plan: allowance framing, no model name anywhere', async () => {
    mockAccount({ plan: 'starter', has_billing_account: true, byok: false });
    render(<GenerationCostLine {...PROPS} />);
    const line = await screen.findByText(
      /used \$0\.0043 of your monthly ai allowance/i
    );
    expect(line).toHaveAttribute('title', '1500 tokens · 2.4s');
    expect(screen.queryByText(/generated for/i)).not.toBeInTheDocument();
  });

  it('BYOK: raw cost with the model in the tooltip — it is their own spend', async () => {
    mockAccount({ plan: 'free', has_billing_account: false, byok: true });
    render(<GenerationCostLine {...PROPS} />);
    const line = await screen.findByText(/generated for \$0\.0043/i);
    await waitFor(() =>
      expect(line).toHaveAttribute(
        'title',
        '1500 tokens · anthropic/claude-sonnet-4.6 · 2.4s'
      )
    );
  });

  it('billing endpoint 404 (self-host): keeps the raw pre-#867 display', async () => {
    mockAccount(null, false);
    render(<GenerationCostLine {...PROPS} />);
    expect(
      await screen.findByText(/generated for \$0\.0043/i)
    ).toBeInTheDocument();
    expect(screen.queryByText(/monthly ai allowance/i)).not.toBeInTheDocument();
  });
});
