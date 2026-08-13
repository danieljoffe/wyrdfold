/**
 * The one salary formatter (#606). The 2026-08-05 drive counted seven
 * coexisting formats on /jobs and /search ("$118,600.00 - $195,680.00",
 * "$191,000 — $253,000 USD", "$119,978.10 - $135,000", "$156,800.00 –
 * $235,200.00/yr", "$119k–$179k", …) because each surface rendered the
 * board's raw ``salary_text``. Structured fields exist since #528/#529 —
 * every surface now renders them through this formatter and falls back to
 * the raw text only when parsing produced nothing.
 *
 * Shape: "$118.6k–$195.7k" for yearly, "$45–$60/hr" for hourly.
 */

export interface SalaryFields {
  min: number | null | undefined;
  max: number | null | undefined;
  currency: string | null | undefined;
  /** API vocabulary: "yearly" | "hourly" | null (never guessed). */
  period: string | null | undefined;
}

// Unambiguous symbols only. CAD/AUD/etc. share "$", so they keep their code
// (rendered once, before the range) rather than a misleading bare dollar.
const CURRENCY_SYMBOLS: Record<string, string> = {
  USD: '$',
  EUR: '€',
  GBP: '£',
};

/** How to prefix an amount: a symbol repeats on both bounds ("$54k–$75k");
 *  a code renders ONCE before the range ("PLN 54k–75k") — the sweep found
 *  "EUR 54k–EUR 75k" on prod (§D4), which doubles the code noise. */
function currencyStyle(currency: string | null | undefined): {
  prefix: string;
  repeats: boolean;
} {
  if (!currency) return { prefix: '$', repeats: true };
  const sym = Object.prototype.hasOwnProperty.call(CURRENCY_SYMBOLS, currency)
    ? CURRENCY_SYMBOLS[currency]
    : null;
  if (sym) return { prefix: sym, repeats: true };
  return { prefix: `${currency} `, repeats: false };
}

function bare(n: number, period: string | null | undefined): string {
  if (period === 'hourly') return String(n);
  // Yearly (and unknown-period) figures compact to k with one decimal,
  // dropping ".0" — 118.6k, 195k.
  const k = n / 1000;
  const rounded = Math.round(k * 10) / 10;
  const text = Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
  return `${text}k`;
}

export function formatSalaryRange(fields: SalaryFields): string | null {
  const { min, max, currency, period } = fields;
  if (min == null && max == null) return null;
  const suffix = period === 'hourly' ? '/hr' : '';
  const { prefix, repeats } = currencyStyle(currency);
  if (min != null && max != null) {
    if (min === max) return `${prefix}${bare(min, period)}${suffix}`;
    return repeats
      ? `${prefix}${bare(min, period)}–${prefix}${bare(max, period)}${suffix}`
      : `${prefix}${bare(min, period)}–${bare(max, period)}${suffix}`;
  }
  if (min != null) return `${prefix}${bare(min, period)}+${suffix}`;
  return `Up to ${prefix}${bare(max as number, period)}${suffix}`;
}

/**
 * Structured-first display for any row carrying the #528 salary columns;
 * raw ``salary_text`` only when parsing produced nothing (e.g. the one
 * known corrupted prod row).
 */
export function formatJobSalary(job: {
  salary_min?: number | null;
  salary_max?: number | null;
  salary_currency?: string | null;
  salary_period?: string | null;
  salary_text?: string | null;
}): string | null {
  return (
    formatSalaryRange({
      min: job.salary_min ?? null,
      max: job.salary_max ?? null,
      currency: job.salary_currency ?? null,
      period: job.salary_period ?? null,
    }) ??
    job.salary_text ??
    null
  );
}
