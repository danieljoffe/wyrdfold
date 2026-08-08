'use client';

/**
 * Locale-dependent values that are DELIBERATELY different on server and client.
 *
 * THE BUG THESE EXIST TO FIX (prod, 2026-08-08): `/targets` threw
 * **React #418 — "text content does not match server-rendered HTML"** on every
 * load. `TargetCard` rendered `new Date(target.updated_at).toLocaleDateString()`.
 * With no explicit locale or timeZone, `toLocaleDateString` resolves against the
 * *host's* settings — and the two hosts disagree:
 *
 *     updated_at        2026-08-08T06:50:44Z
 *     server (UTC)      "8/8/2026"
 *     browser (LA)      "8/7/2026"     <- different day
 *     browser (de-DE)   "8.8.2026"     <- different separator/order
 *
 * React sees the text differ, discards the server HTML for that subtree, and
 * re-renders it on the client — a real cost, and it fills error tracking with a
 * minified stack that looks like a crash but is a formatting mismatch.
 * `Number.prototype.toLocaleString` has exactly the same problem
 * (`"12,345"` vs `"12.345"`).
 *
 * WHY `suppressHydrationWarning` RATHER THAN FORMATTING IN UTC:
 * these are "when did this happen to *me*" labels, so the user's own timezone is
 * the correct thing to show. Pinning them to UTC would silence React by showing
 * US users the wrong day every evening. React documents this attribute for
 * precisely this case — intentionally-divergent content like timestamps — and it
 * applies to the text of the element it sits on, which is why each helper below
 * renders its own leaf `<span>` rather than expecting callers to remember it.
 *
 * RULE: never call `toLocaleDateString` / `toLocaleTimeString` /
 * `toLocaleString` directly in a rendered component. Use these.
 * `src/components/__tests__/LocalFormat.spec.tsx` pins that.
 */

interface DateProps {
  /** ISO-8601 timestamp, or anything `new Date()` accepts. */
  value: string | number | Date | null | undefined;
  /** Rendered when the value is missing or unparseable. */
  fallback?: string;
  options?: Intl.DateTimeFormatOptions;
  className?: string;
}

function toDate(value: DateProps['value']): Date | null {
  if (value === null || value === undefined || value === '') return null;
  const d = value instanceof Date ? value : new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** A date in the viewer's own locale and timezone. */
export function LocalDate({
  value,
  fallback = '—',
  options,
  className,
}: DateProps) {
  const d = toDate(value);
  return (
    <span className={className} suppressHydrationWarning>
      {d ? d.toLocaleDateString(undefined, options) : fallback}
    </span>
  );
}

/** A date *and* time in the viewer's own locale and timezone. */
export function LocalDateTime({
  value,
  fallback = '—',
  options,
  className,
}: DateProps) {
  const d = toDate(value);
  return (
    <span className={className} suppressHydrationWarning>
      {d ? d.toLocaleString(undefined, options) : fallback}
    </span>
  );
}

/** A number grouped per the viewer's locale ("12,345" / "12.345"). */
export function LocalNumber({
  value,
  fallback = '—',
  options,
  className,
}: {
  value: number | null | undefined;
  fallback?: string;
  options?: Intl.NumberFormatOptions;
  className?: string;
}) {
  const ok = typeof value === 'number' && Number.isFinite(value);
  return (
    <span className={className} suppressHydrationWarning>
      {ok ? value.toLocaleString(undefined, options) : fallback}
    </span>
  );
}

/**
 * String form, for the handful of places a formatted date must go into an
 * attribute (`aria-label`, `title`) where a `<span>` cannot.
 *
 * Attributes do NOT trigger React's text-hydration check the way children do,
 * so this is safe — but keep it for attributes only; in element children the
 * components above are what prevent the mismatch.
 */
export function formatLocalDateTime(
  value: DateProps['value'],
  options?: Intl.DateTimeFormatOptions
): string {
  const d = toDate(value);
  return d ? d.toLocaleString(undefined, options) : '';
}
