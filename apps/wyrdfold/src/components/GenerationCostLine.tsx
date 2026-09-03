'use client';

import { useEffect, useState } from 'react';
import { Text } from '@danieljoffe/shared-ui/Text';

/**
 * The per-generation cost line on the resume / cover-letter review pages,
 * extracted from the two identical copies and made payer-aware (#867).
 *
 * What a generation "cost" MEANS depends on who paid:
 * - BYOK: the user's own OpenRouter key was billed — the raw provider cost
 *   and model name are genuinely theirs to see (unchanged presentation).
 * - Managed (starter/pro/trial): the user pays a subscription, not
 *   per-generation; the same number is the OPERATOR's provider cost. Frame
 *   it as allowance consumption and keep the model name out of the tooltip
 *   (unit economics on a public-repo product; the user-relevant telemetry —
 *   tokens and latency — stays).
 * - Billing endpoint 404 (self-host / billing not configured): the operator
 *   and the user are typically the same person; keep the raw display.
 *
 * The payer read is best-effort: until it resolves (or on failure) the raw
 * form renders, matching the pre-#867 behavior.
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
  const [byokPays, setByokPays] = useState<boolean | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch('/api/billing/account')
      .then(res => (res.ok ? res.json() : null))
      .then((data: { byok?: boolean } | null) => {
        if (!cancelled && data && typeof data.byok === 'boolean') {
          setByokPays(data.byok);
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

  if (byokPays === false) {
    return (
      <Text variant='meta' as='span' title={`${tokens} · ${latency}`}>
        Used ${costUsd.toFixed(4)} of your monthly AI allowance
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
