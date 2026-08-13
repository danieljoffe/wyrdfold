import React from 'react';
import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';
import ConversationChat from '../ConversationChat';

const ORIGINAL_FETCH = global.fetch;

beforeEach(() => {
  jest.clearAllMocks();
});

afterAll(() => {
  global.fetch = ORIGINAL_FETCH;
});

describe('ConversationChat', () => {
  it('shows the Thinking spinner while the initial probe is loading', () => {
    // Hanging fetch — never resolves, so the loading state persists.
    global.fetch = jest
      .fn()
      .mockImplementation(
        () => new Promise(() => undefined)
      ) as unknown as typeof fetch;

    render(
      <ConversationChat onComplete={() => undefined} onSkip={() => undefined} />
    );

    expect(screen.getByLabelText(/thinking/i)).toBeInTheDocument();
  });

  it('renders the heading and the skip controls', () => {
    global.fetch = jest
      .fn()
      .mockImplementation(
        () => new Promise(() => undefined)
      ) as unknown as typeof fetch;

    render(
      <ConversationChat onComplete={() => undefined} onSkip={() => undefined} />
    );

    expect(
      screen.getByRole('heading', { level: 2, name: /build your profile/i })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /skip this question/i })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /skip for now/i })
    ).toBeInTheDocument();
  });

  it('renders the assistant probe message once the initial fetch resolves', async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ question: 'Tell me about your most recent role.' }),
    }) as unknown as typeof fetch;

    render(
      <ConversationChat onComplete={() => undefined} onSkip={() => undefined} />
    );

    expect(
      await screen.findByText(/tell me about your most recent role/i)
    ).toBeInTheDocument();
  });

  it('shows an error state when the probe fetch fails', async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: false,
      json: async () => ({}),
    }) as unknown as typeof fetch;

    render(
      <ConversationChat onComplete={() => undefined} onSkip={() => undefined} />
    );

    expect(
      await screen.findByText(/could not start conversation/i)
    ).toBeInTheDocument();
  });

  it('shows the gap context with the opening question (§A4)', async () => {
    // The probe endpoint has always returned the gap — the FE just dropped
    // it, so questions like "…what was the average lift from those tests?"
    // opened with no referent (ux-sweep 2026-08-12 §A4).
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        question: 'What was the average lift from those tests?',
        gap: {
          kind: 'outcome.missing_metric',
          ref: 'Built A/B testing infrastructure',
          context:
            "Outcome lacks a quantified metric: 'Built A/B testing infrastructure'",
        },
      }),
    }) as unknown as typeof fetch;

    render(
      <ConversationChat onComplete={() => undefined} onSkip={() => undefined} />
    );

    expect(
      await screen.findByText(/from your saved experience/i)
    ).toBeInTheDocument();
    expect(
      screen.getByText(/built a\/b testing infrastructure/i)
    ).toBeInTheDocument();
  });

  it('shows no context line when the probe has no gap', async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        question: 'Tell me about your most recent role.',
        gap: null,
      }),
    }) as unknown as typeof fetch;

    render(
      <ConversationChat onComplete={() => undefined} onSkip={() => undefined} />
    );

    await screen.findByText(/tell me about your most recent role/i);
    expect(
      screen.queryByText(/from your saved experience/i)
    ).not.toBeInTheDocument();
  });

  it('marks the textarea with data-sentry-mask for PII redaction', async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ question: 'Hi.' }),
    }) as unknown as typeof fetch;

    render(
      <ConversationChat onComplete={() => undefined} onSkip={() => undefined} />
    );

    const textarea = await screen.findByRole('textbox', {
      name: /your response/i,
    });
    expect(textarea).toHaveAttribute('data-sentry-mask');
  });
});
