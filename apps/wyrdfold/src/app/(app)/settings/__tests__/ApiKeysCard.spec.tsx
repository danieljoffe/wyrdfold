import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ApiKeysCard from '../ApiKeysCard';

const mockToast = jest.fn();

jest.mock('@/state/Toast/ToastProvider', () => ({
  useToast: () => ({ toast: mockToast }),
}));

interface KeyMeta {
  provider: string;
  last4: string | null;
  created_at: string;
  updated_at: string;
  rotated_at: string | null;
}

function meta(over: Partial<KeyMeta> = {}): KeyMeta {
  return {
    provider: 'openrouter',
    last4: 'ab12',
    created_at: '2026-06-01T00:00:00Z',
    updated_at: '2026-06-01T00:00:00Z',
    rotated_at: null,
    ...over,
  };
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

describe('ApiKeysCard', () => {
  beforeEach(() => {
    mockToast.mockClear();
    global.fetch = jest.fn() as jest.Mock;
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('shows a muted note when BYOK is unavailable on the instance', async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce(
      jsonOk({ available: false, keys: [] })
    );

    render(<ApiKeysCard />);

    expect(
      await screen.findByText(/isn’t enabled on this instance/i)
    ).toBeInTheDocument();
    // No key form when BYOK is off.
    expect(screen.queryByLabelText(/OpenRouter key/i)).not.toBeInTheDocument();
  });

  it('adds a key: Save is gated until typed, then PUTs and shows masked last4', async () => {
    (global.fetch as jest.Mock)
      .mockResolvedValueOnce(jsonOk({ available: true, keys: [] }))
      .mockResolvedValueOnce(jsonOk(meta()));
    const user = userEvent.setup();

    render(<ApiKeysCard />);

    const input = await screen.findByLabelText(/OpenRouter key/i);
    const save = screen.getByRole('button', { name: /save key/i });
    expect(save).toBeDisabled();

    await user.type(input, 'sk-or-secret');
    expect(save).toBeEnabled();
    await user.click(save);

    await waitFor(() =>
      expect(global.fetch).toHaveBeenLastCalledWith(
        '/api/profile/keys/openrouter',
        expect.objectContaining({ method: 'PUT' })
      )
    );
    // The plaintext goes up in the body; only last4 comes back.
    const putBody = JSON.parse(
      (global.fetch as jest.Mock).mock.calls[1][1].body as string
    );
    expect(putBody).toEqual({ key: 'sk-or-secret' });
    expect(await screen.findByText(/•••• ab12/)).toBeInTheDocument();
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({ variant: 'success' })
    );
  });

  it('renders the masked key + rotation date for an existing key', async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce(
      jsonOk({
        available: true,
        keys: [meta({ rotated_at: '2026-06-10T00:00:00Z' })],
      })
    );

    render(<ApiKeysCard />);

    expect(await screen.findByText(/•••• ab12/)).toBeInTheDocument();
    expect(screen.getByText(/Rotated/i)).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /^Rotate$/i })
    ).toBeInTheDocument();
  });

  it('removes an existing key after confirmation → DELETE', async () => {
    (global.fetch as jest.Mock)
      .mockResolvedValueOnce(jsonOk({ available: true, keys: [meta()] }))
      .mockResolvedValueOnce(jsonOk({ deleted: true }));
    const user = userEvent.setup();

    render(<ApiKeysCard />);

    expect(await screen.findByText(/•••• ab12/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /^Remove$/i }));

    const dialog = await screen.findByRole('dialog');
    await user.click(
      within(dialog).getByRole('button', { name: /remove key/i })
    );

    await waitFor(() =>
      expect(global.fetch).toHaveBeenLastCalledWith(
        '/api/profile/keys/openrouter',
        expect.objectContaining({ method: 'DELETE' })
      )
    );
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({ variant: 'success' })
    );
  });

  it('surfaces the upstream error message when saving fails', async () => {
    (global.fetch as jest.Mock)
      .mockResolvedValueOnce(jsonOk({ available: true, keys: [] }))
      .mockResolvedValueOnce(
        jsonError(503, 'This instance has no BYOK master key configured.')
      );
    const user = userEvent.setup();

    render(<ApiKeysCard />);

    const input = await screen.findByLabelText(/OpenRouter key/i);
    await user.type(input, 'sk-or-x');
    await user.click(screen.getByRole('button', { name: /save key/i }));

    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          variant: 'error',
          title: 'This instance has no BYOK master key configured.',
        })
      )
    );
  });
});
