import StatusCard from '@/components/StatusCard';
import LinkButton from '@/components/kit/LinkButton';
import WyrdfoldSidebar from './(app)/WyrdfoldSidebar';

/**
 * Catches unmatched URLs that don't fall inside any route segment.
 *
 * `(app)/not-found.tsx` only fires when a page *inside* the `(app)`
 * group calls `notFound()`. For URLs that match nothing at all (e.g.
 * `/jobs` before that page is ported), Next.js looks for a root-level
 * `not-found.tsx` — this one. The middleware redirects unauthenticated
 * users to `/login` for gated paths, so the typical visitor here is
 * signed in — but NOT every one: unmatched sub-paths under the public
 * allowlist (e.g. `/search/a/b`, which no route matches) reach this
 * file logged-out and see the member shell (#831). Dead listing ids —
 * the common shared-link case — are handled by `search/not-found.tsx`
 * under the public header; only the unmatched-sub-path oddballs remain.
 *
 * Wrap the content in the same sidebar shell ``(app)/layout.tsx``
 * provides so the user keeps direct nav to every authed route from
 * the not-found screen. Without this, the user has only "Back to
 * dashboard" and has to navigate from there to anywhere else.
 */
export default function NotFound() {
  return (
    <div className='flex min-h-screen'>
      <WyrdfoldSidebar />
      <main
        id='main-content'
        className='flex-1 overflow-x-hidden p-4 pb-[calc(theme(spacing.16)+env(safe-area-inset-bottom)+1rem)] md:p-6'
      >
        <div className='flex min-h-full items-center justify-center'>
          <StatusCard
            title='Page not found'
            body={
              "We couldn't find that page. It may have moved, been removed, or never existed."
            }
            actions={
              <LinkButton
                name='wyrdfold-root-not-found-home'
                variant='primary'
                size='sm'
                href='/dashboard'
              >
                Back to dashboard
              </LinkButton>
            }
          />
        </div>
      </main>
    </div>
  );
}
