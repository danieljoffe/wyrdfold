import { Suspense } from 'react';
import type { Metadata } from 'next';

import JobSearchExplorer from './JobSearchExplorer';

export const metadata: Metadata = {
  title: 'Search',
};

export default function SearchPage() {
  // JobSearchExplorer reads the search state from the URL (useSearchParams),
  // which Next requires to sit under a Suspense boundary.
  return (
    <Suspense>
      <JobSearchExplorer />
    </Suspense>
  );
}
