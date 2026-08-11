import type { ReactNode } from 'react';
import AppShell from './AppShell';

// Auth gating for /(app)/* lives entirely in proxy.ts middleware, which runs
// on every matched request (including RSC navigations) and redirects to
// /login when there's no session. Re-doing supabase.auth.getUser() here would
// mean two network round-trips per page render, which serializes the shell
// behind a Supabase call that the middleware already made.
//
// Dynamic-rendering is signalled at the leaf via `await connection()` inside
// `createAuthServerClient` (see lib/supabase/auth-server.ts). Pages calling
// auth opt-in there; the layout itself stays cacheable.
//
// The shell markup itself lives in `AppShell` — shared with the auth-adaptive
// `/search` route so a signed-in visitor sees the identical sidebar there.

export default function AppLayout({ children }: { children: ReactNode }) {
  return <AppShell>{children}</AppShell>;
}
