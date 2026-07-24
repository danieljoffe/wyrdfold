import type { Metadata } from 'next';

import JobSearchExplorer from './JobSearchExplorer';

export const metadata: Metadata = {
  title: 'Search',
};

export default function SearchPage() {
  return <JobSearchExplorer />;
}
