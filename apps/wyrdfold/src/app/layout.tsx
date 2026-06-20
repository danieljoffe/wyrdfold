import type { Metadata, Viewport } from 'next';
import type { ReactNode } from 'react';
import { cookies } from 'next/headers';
import { ToastProvider } from '@/state/Toast/ToastProvider';
import { ThemeProvider } from '@/state/Theme/ThemeProvider';
import {
  THEME_COOKIE,
  THEME_RESOLVED_COOKIE,
  type ThemePreference,
  resolveIsDark,
} from '@/state/Theme/themeCookies';
import './global.css';

export const metadata: Metadata = {
  title: {
    template: '%s | WyrdFold',
    default: 'WyrdFold',
  },
  description: 'AI-assisted job search command center.',
  manifest: '/site.webmanifest',
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
  themeColor: '#8FC900',
};

export default async function RootLayout({
  children,
}: {
  children: ReactNode;
}) {
  // Read the theme preference server-side so the `<html>` class is painted on
  // the first byte — no pre-hydration inline script (and thus no theme-class or
  // CSP-nonce hydration mismatch) and no light-flash. `cookies()` is a dynamic
  // API, so reading it also opts this layout — and every route beneath it —
  // into per-request rendering, which the nonce-based CSP in proxy.ts requires:
  // Next only stamps the per-request nonce onto the scripts it emits when the
  // page renders per request (a statically prerendered page would ship
  // nonce-less scripts and `'strict-dynamic'` would block them).
  const cookieStore = await cookies();
  const prefCookie = cookieStore.get(THEME_COOKIE)?.value;
  const resolvedCookie = cookieStore.get(THEME_RESOLVED_COOKIE)?.value;
  const isDark = resolveIsDark(prefCookie, resolvedCookie);
  const initialTheme: ThemePreference =
    prefCookie === 'light' || prefCookie === 'dark' || prefCookie === 'system'
      ? prefCookie
      : 'system';

  return (
    // ``pyre`` namespaces the design-token reset; ``dark`` is painted here from
    // the cookie (resolved server-side) and kept in sync after hydration by
    // ThemeProvider's class effect.
    <html lang='en' className={isDark ? 'pyre dark' : 'pyre'}>
      <body>
        {/* WCAG 2.4.1 Bypass Blocks — keyboard/SR users skip past the
            sidebar (~7 nav links + sign-out) on every authed route.
            Hidden until focused so it doesn't disturb the visual design. */}
        <a
          href='#main-content'
          className='sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-50 focus:rounded-md focus:bg-brand-500 focus:px-3 focus:py-2 focus:text-sm focus:font-medium focus:text-text-inverse focus:outline-none focus:ring-2 focus:ring-brand-500 focus:ring-offset-2 focus:ring-offset-surface'
        >
          Skip to main content
        </a>
        <ThemeProvider
          initialTheme={initialTheme}
          initialResolvedTheme={isDark ? 'dark' : 'light'}
        >
          <ToastProvider>{children}</ToastProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
