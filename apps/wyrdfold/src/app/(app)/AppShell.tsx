import type { ReactNode } from 'react';
import WyrdfoldSidebar from './WyrdfoldSidebar';

/**
 * The authenticated app shell — sidebar + main content column. Extracted from
 * `(app)/layout.tsx` so the auth-adaptive `/search` route can render the IDENTICAL
 * shell for signed-in visitors while a logged-out visitor gets the lean public
 * header instead (#467 §10). Both `(app)/layout.tsx` and `search/layout.tsx`'s
 * authed branch render this, so the signed-in shell stays byte-for-byte the same
 * across the two entry points (no duplication to drift).
 */
export default function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className='flex min-h-screen'>
      <WyrdfoldSidebar />
      {/*
        Mobile bottom-nav is `position: fixed` at viewport bottom (h-14 + iOS
        safe-area). The earlier "trailing clearance div" approach didn't bite
        for sticky / scroll-end content (pagination on /jobs sat under the
        nav; 4th target card on /targets was clipped). Layout-level padding
        on `<main>` is the defensive fix — anything sticky-bottom inside main
        will dock above the nav, and natural scroll bottoms get the same
        clearance for free.
      */}
      <main
        id='main-content'
        className='flex-1 overflow-x-hidden p-4 pb-[calc(theme(spacing.16)+env(safe-area-inset-bottom)+1rem)] md:p-6'
      >
        {children}
      </main>
    </div>
  );
}
