/**
 * Relative "time ago" label for a job's posting date — the compact, day-granular
 * form used across the jobs surfaces (list rows + cards). Null-safe.
 * Returns "—" (no date), "today" (< 1 day), "1d ago", or "Nd ago".
 */
export function timeAgo(dateStr: string | null): string {
  if (!dateStr) return '—';
  const diff = Date.now() - new Date(dateStr).getTime();
  const days = Math.floor(diff / 86400000);
  if (days === 0) return 'today';
  if (days === 1) return '1d ago';
  return `${days}d ago`;
}
