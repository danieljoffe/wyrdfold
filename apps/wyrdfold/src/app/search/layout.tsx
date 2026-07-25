import type { ReactNode } from 'react';

import AppShell from '@/app/(app)/AppShell';
import { getOptionalUser } from '@/lib/supabase/getUser';

import PublicSearchHeader from './PublicSearchHeader';

/**
 * Auth-adaptive shell for `/search` (#467 §10). This route lives OUTSIDE the
 * `(app)` group precisely so it can serve both audiences at the same URL:
 *
 *  - **Signed in →** the identical app shell (sidebar) via `AppShell`, shared
 *    with `(app)/layout.tsx` so the authed experience is byte-for-byte the same
 *    as every other app page.
 *  - **Logged out →** a lean public header + a calm "Sign up free" CTA. The
 *    proxy middleware allowlists `/search` (like `/terms`·`/privacy`), so a
 *    logged-out visitor reaches this page instead of being bounced to `/login`.
 *    Every OTHER `(app)/*` route stays gated.
 *
 * `getOptionalUser()` is `cache()`-wrapped, so this layout and the page share a
 * single `auth.getUser()` verification for the request (the page reads it too,
 * to thread `isAuthenticated` into the rendering). It never throws — a failure
 * resolves to logged-out, i.e. the public surface (fail-closed, never a crash).
 */
export default async function SearchLayout({
  children,
}: {
  children: ReactNode;
}) {
  const user = await getOptionalUser();

  if (user) {
    return <AppShell>{children}</AppShell>;
  }

  return (
    <div className='flex min-h-screen flex-col bg-surface text-text-primary'>
      <PublicSearchHeader />
      <main
        id='main-content'
        className='mx-auto w-full max-w-6xl flex-1 overflow-x-hidden px-4 py-6 md:px-6 md:py-8'
      >
        {children}
      </main>
    </div>
  );
}
