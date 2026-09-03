'use client';

import { useEffect, useState } from 'react';
import { Text } from '@danieljoffe/shared-ui/Text';

/**
 * The per-generation cost line on the resume / cover-letter review pages,
 * extracted from the two identical copies and made payer-aware (#867).
 *
 * The payer state is #991's ``key_source`` — the same resolution the budget
 * gates enforce — NOT reconstructed from the ``byok`` boolean: byok=false
 * alone conflates a managed payer with the payer-less saas-free state, and
 * the first cut of this component told a canceled subscriber that historical
 * generations "used your monthly allowance" their account no longer has
 * (the #993 review's blocker).
 *
 * - "user" (BYOK): the user's own OpenRouter key was billed — raw provider
 *   cost and model name are genuinely theirs to see.
 * - "host" (managed/trial): the user pays a subscription, not
 *   per-generation; frame the number as allowance consumption and keep the
 *   model name out of the tooltip (unit economics on a public-repo product;
 *   tokens and latency stay).
 * - "none" (saas free, no usable key): NO allowance exists — state the
 *   historical cost neutrally without calling it one.
 * - key_source absent (pre-#991 API in a mixed-deploy window), endpoint 404
 *   (self-host / billing off), or fetch failure: the raw pre-#867 display.
 */
export default function GenerationCostLine({
  costUsd,
  inputTokens,
  outputTokens,
  model,
  latencyMs,
}: {
  costUsd: number;
  inputTokens: number;
  outputTokens: number;
  model: string | null;
  latencyMs: number;
}) {
  const [keySource, setKeySource] = useState<'host' | 'user' | 'none' | null>(
    null
  );

  useEffect(() => {
    let cancelled = false;
    fetch('/api/billing/account')
      .then(res => (res.ok ? res.json() : null))
      .then((data: { key_source?: string } | null) => {
        const ks = data?.key_source;
        if (!cancelled && (ks === 'host' || ks === 'user' || ks === 'none')) {
          setKeySource(ks);
        }
      })
      .catch(() => {
        // Stay on the raw form — correct for self-host, harmless elsewhere.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const tokens = `${inputTokens + outputTokens} tokens`;
  const latency = `${(latencyMs / 1000).toFixed(1)}s`;

  if (keySource === 'host') {
    return (
      <Text variant='meta' as='span' title={`${tokens} · ${latency}`}>
        Used ${costUsd.toFixed(4)} of your monthly AI allowance
      </Text>
    );
  }

  if (keySource === 'none') {
    // Historical host-paid generation viewed by an account with no payer
    // today (e.g. a canceled subscriber): a true statement of what it cost,
    // never an allowance claim. Model name stays out — same operator-
    // internals reasoning as the host branch.
    return (
      <Text variant='meta' as='span' title={`${tokens} · ${latency}`}>
        Generation cost: ${costUsd.toFixed(4)}
      </Text>
    );
  }

  return (
    <Text
      variant='meta'
      as='span'
      title={`${tokens} · ${model ?? 'unknown model'} · ${latency}`}
    >
      Generated for ${costUsd.toFixed(4)}
    </Text>
  );
}
