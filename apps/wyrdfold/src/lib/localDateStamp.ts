/**
 * Today's date as YYYY-MM-DD in the USER'S timezone (ux-sweep 2026-08-12 §A2).
 *
 * `new Date().toISOString().slice(0, 10)` is the UTC date — in any western
 * timezone an evening session stamps download filenames with TOMORROW's date.
 */
export function localDateStamp(now: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}
