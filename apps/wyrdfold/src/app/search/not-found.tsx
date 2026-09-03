import StatusCard from '@/components/StatusCard';
import LinkButton from '@/components/kit/LinkButton';

/**
 * Catches `notFound()` from `/search/[id]` — a dead or never-existing
 * listing id. Renders inside `search/layout.tsx`, which is auth-adaptive:
 * a logged-out visitor gets the public header, a signed-in user gets the
 * app shell. Before this file existed the root `not-found.tsx` caught
 * these and showed the signed-in sidebar shell (with links that all bounce
 * to /login) to logged-out visitors following a shared listing URL (#831).
 *
 * Listings die routinely — companies fill roles and boards drop postings —
 * so this is a normal destination for a stale shared link, not an error
 * page. Keep the way forward prominent: back to the live search.
 */
export default function SearchNotFound() {
  return (
    <div className='flex min-h-[60vh] items-center justify-center px-4 py-12'>
      <StatusCard
        title='Listing not found'
        body='This listing has expired or was removed — postings come and go as companies fill roles. Browse current listings instead.'
        actions={
          <LinkButton
            name='wyrdfold-search-not-found-browse'
            variant='primary'
            size='sm'
            href='/search'
          >
            Browse jobs
          </LinkButton>
        }
      />
    </div>
  );
}
