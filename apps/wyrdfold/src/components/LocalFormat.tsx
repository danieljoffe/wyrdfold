'use client';

import { useEffect, useState } from 'react';

/**
 * Locale-dependent values that are DELIBERATELY different on server and client.
 *
 * THE ORIGINAL BUG (prod, 2026-08-08): `/targets` threw **React #418 — "text
 * content does not match server-rendered HTML"** on every load. `TargetCard`
 * rendered `new Date(target.updated_at).toLocaleDateString()`. With no explicit
 * locale or timeZone, `toLocaleDateString` resolves against the *host's*
 * settings — and the two hosts disagree:
 *
 *     updated_at        2026-08-08T06:50:44Z
 *     server (UTC)      "8/8/2026"
 *     browser (LA)      "8/7/2026"     <- different day
 *     browser (de-DE)   "8.8.2026"     <- different separator/order
 *
 * `Number.prototype.toLocaleString` has exactly the same problem
 * ("12,345" vs "12.345").
 *
 * THE SECOND BUG (prod, 2026-08-14) — why this file no longer relies on
 * `suppressHydrationWarning` alone. That attribute stops React *warning* about
 * a text mismatch, and also stops it *correcting* one: the server's HTML is
 * kept and never replaced, because nothing re-renders the subtree afterwards.
 * So every date froze at the server's UTC rendering. Measured live:
 *
 *     browser local   Fri Aug 14 2026 23:37 GMT-0700
 *     UTC             2026-08-15T06:37Z
 *     card rendered   "8/15/2026"    <- TOMORROW, to the user
 *
 * For every user west of UTC that is a date in the future, on every card, every
 * evening. The old comment here argued against pinning to UTC because it "would
 * silence React by showing US users the wrong day every evening";
 * `suppressHydrationWarning` was quietly producing that exact outcome.
 *
 * HOW IT WORKS NOW — two renders, both correct for their moment:
 *
 *   1. Server + first client render: an explicitly pinned locale and timezone.
 *      Deterministic, so both hosts produce the same string and hydration has
 *      nothing to reconcile.
 *   2. After mount: the viewer's own locale and timezone. This is an ordinary
 *      React update, not hydration, so the DOM text is genuinely replaced.
 *
 * `suppressHydrationWarning` is kept as a backstop for any residual divergence
 * in step 1 — with step 2 always firing, it can no longer freeze a wrong value.
 *
 * RULE: never call `toLocaleDateString` / `toLocaleTimeString` /
 * `toLocaleString` directly in a rendered component. Use these.
 * `src/components/__tests__/LocalFormat.spec.tsx` pins that.
 */

/**
 * Locale used for the pre-hydration render. Any fixed value works — it only has
 * to be the SAME on both hosts. It is visible for one frame before the viewer's
 * own locale takes over.
 */
const SSR_LOCALE = 'en-US';

/** False on the server and for the hydrating render; true from mount onwards. */
function useHydrated(): boolean {
  const [hydrated, setHydrated] = useState(false);
  useEffect(() => setHydrated(true), []);
  return hydrated;
}

/**
 * Pin the timezone for the pre-hydration pass — unless the caller already pinned
 * one, in which case their choice is deterministic and is left alone.
 */
function ssrDateOptions(
  options?: Intl.DateTimeFormatOptions
): Intl.DateTimeFormatOptions {
  return { ...options, timeZone: options?.timeZone ?? 'UTC' };
}

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
  const hydrated = useHydrated();
  const d = toDate(value);
  return (
    <span className={className} suppressHydrationWarning>
      {d
        ? hydrated
          ? d.toLocaleDateString(undefined, options)
          : d.toLocaleDateString(SSR_LOCALE, ssrDateOptions(options))
        : fallback}
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
  const hydrated = useHydrated();
  const d = toDate(value);
  return (
    <span className={className} suppressHydrationWarning>
      {d
        ? hydrated
          ? d.toLocaleString(undefined, options)
          : d.toLocaleString(SSR_LOCALE, ssrDateOptions(options))
        : fallback}
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
  const hydrated = useHydrated();
  const ok = typeof value === 'number' && Number.isFinite(value);
  return (
    <span className={className} suppressHydrationWarning>
      {ok
        ? hydrated
          ? value.toLocaleString(undefined, options)
          : value.toLocaleString(SSR_LOCALE, options)
        : fallback}
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
