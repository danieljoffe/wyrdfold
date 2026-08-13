import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import NearMissCard from '../NearMissCard';
import type { NearMissInsights } from '../types';

const ORIGINAL_FETCH = global.fetch;

afterAll(() => {
  global.fetch = ORIGINAL_FETCH;
});

function mockFetch(body: unknown, ok = true) {
  global.fetch = jest.fn().mockResolvedValue({
    ok,
    status: ok ? 200 : 500,
    json: async () => body,
  }) as unknown as typeof fetch;
}

function payload(overrides: Partial<NearMissInsights> = {}): NearMissInsights {
  return {
    targets: [
      {
        target_id: 't-1',
        label: 'Staff Frontend Engineer',
        titles: [
          {
            title: 'staff platform engineer',
            confidence: 60,
            last_judged_at: '2026-08-13T00:00:00Z',
          },
          {
            title: 'senior web engineer',
            confidence: 70,
            last_judged_at: '2026-08-13T00:00:00Z',
          },
        ],
      },
      // A target with nothing near-missed still arrives from the API —
      // the card must not render an empty group for it.
      { target_id: 't-2', label: 'CX Lead', titles: [] },
    ],
    confidence_ceiling: 80,
    window_days: 30,
    ...overrides,
  };
}

describe('NearMissCard', () => {
  it('renders near-miss titles with confidence, grouped by target, omitting empty targets', async () => {
    mockFetch(payload());
    render(<NearMissCard />);

    // Await the loaded content (the "Almost Matched" heading also exists in
    // the loading skeleton, so it can't be the readiness signal).
    expect(
      await screen.findByText(/staff platform engineer/i)
    ).toBeInTheDocument();
    // Exact casing: the card smart-title-cases title_norm (which arrives
    // lowercased) — an /i regex alone would not catch this regressing.
    expect(screen.getByText('Staff Platform Engineer')).toBeInTheDocument();
    expect(screen.getByText(/almost matched/i)).toBeInTheDocument();
    expect(screen.getByText(/staff frontend engineer/i)).toBeInTheDocument();
    expect(screen.getByText('60%')).toBeInTheDocument();
    expect(screen.getByText('70%')).toBeInTheDocument();
    // The empty target contributes no group.
    expect(screen.queryByText(/cx lead/i)).not.toBeInTheDocument();
  });

  it('renders nothing when every target has an empty list (the healthy steady state)', async () => {
    mockFetch(
      payload({
        targets: [{ target_id: 't-1', label: 'FE', titles: [] }],
      })
    );
    const { container } = render(<NearMissCard />);

    await waitFor(() => {
      expect(container).toBeEmptyDOMElement();
    });
  });

  it('renders nothing on a failed fetch — advisory card, never an error banner', async () => {
    mockFetch({ detail: 'boom' }, false);
    const { container } = render(<NearMissCard />);

    await waitFor(() => {
      expect(container).toBeEmptyDOMElement();
    });
  });

  it('renders nothing on a malformed payload (shape guard)', async () => {
    mockFetch({ nonsense: true });
    const { container } = render(<NearMissCard />);

    await waitFor(() => {
      expect(container).toBeEmptyDOMElement();
    });
  });
});
