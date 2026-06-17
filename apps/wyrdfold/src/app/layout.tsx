import type { Metadata, Viewport } from 'next';
import type { ReactNode } from 'react';
import { headers } from 'next/headers';
import { ToastProvider } from '@/state/Toast/ToastProvider';
import { ThemeProvider } from '@/state/Theme/ThemeProvider';
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
  // Reading a request header opts this layout — and therefore every route
  // beneath it — into per-request dynamic rendering. That's a hard
  // requirement for the nonce-based CSP set in proxy.ts: Next only stamps the
  // per-request nonce onto the scripts it emits when the page renders per
  // request, and the inline theme script below needs that same nonce to be
  // authorized. A statically prerendered / CDN-cached page would ship HTML
  // whose scripts carry no nonce, so `'strict-dynamic'` would block every one.
  const nonce = (await headers()).get('x-nonce') ?? undefined;

  return (
    // ``pyre`` namespaces the design-token reset; ``ThemeProvider``
    // toggles ``dark`` on the html element based on the stored theme
    // preference (system / light / dark). To avoid a light-flash for
    // users whose preference is dark (the typical OS default for
    // this app), the inline script tag below sets the class
    // synchronously before React hydrates.
    <html lang='en' className='pyre'>
      <head>
        <script
          nonce={nonce}
          dangerouslySetInnerHTML={{
            __html: `(() => {
  try {
    const stored = localStorage.getItem('theme');
    const isDark = stored === 'dark' || (stored !== 'light' && window.matchMedia('(prefers-color-scheme: dark)').matches);
    if (isDark) document.documentElement.classList.add('dark');
  } catch (_) {}
})();`,
          }}
        />
      </head>
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
        <ThemeProvider>
          <ToastProvider>{children}</ToastProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
