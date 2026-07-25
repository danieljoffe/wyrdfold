import type { Metadata } from 'next';
import { notFound } from 'next/navigation';

import { getOptionalUser } from '@/lib/supabase/getUser';

import { fetchListing } from './fetchListing';
import ListingPageContent from './ListingPageContent';

/**
 * Hard-load `/search/[id]` (#467 §11.2 fast-follow) — the shareable listing
 * URL as a standalone page. A SOFT navigation from the grid never reaches
 * this: the `@modal/(.)[id]` interception renders the detail as a modal over
 * the still-mounted grid instead. This page is the direct-hit path: a pasted
 * link, a refresh, a new tab. Same auth-adaptive layout as `/search` (app
 * shell signed-in, public header logged-out), same detail body as the modal.
 *
 * A 404/junk id renders the app's not-found page (`notFound()`); the API is
 * the authority on eligibility, so a delisted/non-US row 404s identically to
 * a missing one (no existence leak, mirroring the endpoint).
 */

export async function generateMetadata({
  params,
}: {
  params: Promise<{ id: string }>;
}): Promise<Metadata> {
  const { id } = await params;
  // Title from the role when the read succeeds; NEVER fail metadata over a
  // fetch problem — the page render below owns the 404/error decision. The
  // duplicate fetch collapses via React's per-render fetch memoization.
  let title = 'Job listing';
  try {
    const listing = await fetchListing(id);
    if (listing) title = `${listing.title} at ${listing.company_name}`;
  } catch {
    // keep the fallback title
  }
  return {
    title,
    // NOINDEX (#467 §3/§10 — SEO deferred, same stance as /search itself):
    // listing-level indexing is a duplicate-content / freshness-churn trap.
    robots: { index: false, follow: false },
  };
}

export default async function ListingPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const [user, listing] = await Promise.all([
    getOptionalUser(),
    fetchListing(id),
  ]);
  if (!listing) notFound();

  return <ListingPageContent job={listing} isAuthenticated={user !== null} />;
}
