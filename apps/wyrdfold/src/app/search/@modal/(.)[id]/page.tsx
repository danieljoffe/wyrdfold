import { getOptionalUser } from '@/lib/supabase/getUser';

import ListingModal from '@/app/search/ListingModal';

/**
 * Intercepting route for the listing detail (#467 §11.2 fast-follow): a SOFT
 * navigation to `/search/[id]` (a card click on the grid) matches here and
 * renders the detail as a modal in the `@modal` slot — the grid stays mounted
 * as `children` underneath, scroll and search state intact, and the URL is
 * the real, shareable `/search/<id>`. A HARD load of the same URL skips
 * interception entirely and renders `search/[id]/page.tsx` as a full page.
 *
 * `getOptionalUser()` is the same `cache()`-shared read the layout/page use:
 * the modal's rendering (bind→unlock vs the soft signup allusion) branches on
 * presence, exactly like the grid (#467 §10).
 */
export default async function InterceptedListingPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const user = await getOptionalUser();
  return <ListingModal id={id} isAuthenticated={user !== null} />;
}
