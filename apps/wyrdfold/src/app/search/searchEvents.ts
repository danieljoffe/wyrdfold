/**
 * Client-side search-funnel beacon (#467 §10 PR6).
 *
 * Fires the two ticks only the browser can see — a card opened into the
 * detail modal, the logged-out detail's "Sign up free" click — at the BFF's
 * `/api/search-events`, which relays them (BFF secret + trusted IP) to the
 * identity-free `search_events` ledger.
 *
 * STRICTLY fire-and-forget: never awaited, never surfaced, never allowed to
 * throw into UI code — a failed beacon costs one metric row, nothing else.
 * `keepalive` lets the `signup_click` tick survive the navigation to /login.
 */
export function emitSearchEvent(event: {
  event_type: 'card_open' | 'signup_click';
  surface: 'public' | 'authed';
  job_posting_id?: string;
}): void {
  try {
    void fetch('/api/search-events', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(event),
      keepalive: true,
    }).catch(() => {
      // Dropped tick — deliberately silent.
    });
  } catch {
    // e.g. no fetch in an exotic embedder — analytics must never break UI.
  }
}
