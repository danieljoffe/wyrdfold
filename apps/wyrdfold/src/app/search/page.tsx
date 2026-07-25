import { Suspense } from 'react';
import type { Metadata } from 'next';

import { getOptionalUser } from '@/lib/supabase/getUser';

import JobSearchExplorer from './JobSearchExplorer';

export const metadata: Metadata = {
  title: 'Search',
  // NOINDEX (#467 §3/§10 — SEO deferred). Listing-level indexing is a trap
  // (duplicate content vs. the source ATS, freshness churn, redistribution
  // exposure). Keep the public search surface out of the index until/unless we
  // ship quality-gated aggregate landing pages.
  robots: { index: false, follow: false },
};

export default async function SearchPage() {
  // Read the user once (shared with the layout via cache()) to decide the
  // rendering: logged-out gets the public surface (public fetch, no membership,
  // no per-card footer, a soft signup allusion in the detail); signed-in is the
  // full bind→unlock experience, unchanged.
  const user = await getOptionalUser();

  // JobSearchExplorer reads the search state from the URL (useSearchParams),
  // which Next requires to sit under a Suspense boundary.
  return (
    <Suspense>
      <JobSearchExplorer isAuthenticated={user !== null} />
    </Suspense>
  );
}
