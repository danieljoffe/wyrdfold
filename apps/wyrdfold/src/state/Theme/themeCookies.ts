/**
 * Theme persistence via cookies so the server can paint the correct `<html>`
 * class on the first byte — no pre-hydration inline script, no class/nonce
 * hydration mismatch, no theme flash.
 *
 * Two cookies:
 * - `theme`          the user's preference: light | dark | system.
 * - `theme-resolved` the concrete value to paint NOW (light | dark). The client
 *                    keeps this fresh so the server can resolve `system` (whose
 *                    OS `prefers-color-scheme` it can't read) to the last-known
 *                    value.
 *
 * `resolveIsDark` is server-safe (no `document`); the writer is a no-op off the
 * browser, so this module is importable from the server layout.
 */

export const THEME_COOKIE = 'theme';
export const THEME_RESOLVED_COOKIE = 'theme-resolved';

export type ThemePreference = 'light' | 'dark' | 'system';
export type ResolvedTheme = 'light' | 'dark';

// One year, site-wide, Lax — a UI preference, not a credential, so no need for
// HttpOnly (the client must write it) and Lax is plenty.
const COOKIE_ATTRS = 'path=/; max-age=31536000; samesite=lax';

/**
 * Resolve whether to paint dark from the two raw cookie values. Explicit
 * light/dark wins; `system` (or an unset/garbage preference) falls back to the
 * client-cached resolved value, defaulting to light.
 */
export function resolveIsDark(
  pref: string | undefined,
  resolved: string | undefined
): boolean {
  if (pref === 'dark') return true;
  if (pref === 'light') return false;
  return resolved === 'dark';
}

/** Whether a `theme` cookie is already set (used to gate one-time migration). */
export function hasThemeCookie(): boolean {
  if (typeof document === 'undefined') return false;
  return document.cookie
    .split('; ')
    .some(c => c.startsWith(`${THEME_COOKIE}=`));
}

/** Persist preference + resolved value. No-op outside the browser. */
export function writeThemeCookies(
  pref: ThemePreference,
  resolved: ResolvedTheme
): void {
  if (typeof document === 'undefined') return;
  document.cookie = `${THEME_COOKIE}=${pref}; ${COOKIE_ATTRS}`;
  document.cookie = `${THEME_RESOLVED_COOKIE}=${resolved}; ${COOKIE_ATTRS}`;
}
