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

function symbol(currency: string | null | undefined): string {
  return !currency || currency === 'USD' ? '$' : `${currency} `;
}

function amount(
  n: number,
  currency: string | null | undefined,
  period: string | null | undefined
): string {
  if (period === 'hourly') return `${symbol(currency)}${n}`;
  // Yearly (and unknown-period) figures compact to k with one decimal,
  // dropping ".0" — $118.6k, $195k.
  const k = n / 1000;
  const rounded = Math.round(k * 10) / 10;
  const text = Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
  return `${symbol(currency)}${text}k`;
}

export function formatSalaryRange(fields: SalaryFields): string | null {
  const { min, max, currency, period } = fields;
  if (min == null && max == null) return null;
  const suffix = period === 'hourly' ? '/hr' : '';
  if (min != null && max != null) {
    return min === max
      ? `${amount(min, currency, period)}${suffix}`
      : `${amount(min, currency, period)}–${amount(max, currency, period)}${suffix}`;
  }
  if (min != null) return `${amount(min, currency, period)}+${suffix}`;
  return `Up to ${amount(max as number, currency, period)}${suffix}`;
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
