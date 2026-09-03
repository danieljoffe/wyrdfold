import Link from 'next/link';

import LinkButton from '@/components/kit/LinkButton';
import WyrdfoldWordmark from '@/components/WyrdfoldWordmark';

/**
 * Lean public header for the logged-out `/search` surface (#467 §10). Not the
 * full marketing shell (`(public)/layout.tsx`) and not the app sidebar — just a
 * wordmark home-link and one calm conversion path. The stronger, single
 * conversion moment lives in the listing detail (§11.5); the header stays
 * quiet — a "Sign in" ghost + the "Get early access" CTA, both to `/login`
 * (`/login` is the one door for both: invited → sign in, stranger → the
 * waitlist link, #835).
 *
 * Copy note (#971 §3): "Get early access" because signup is CLOSED — the old
 * "Sign up free" promised something the invite wall refused, and the visitor
 * met three different promises for one door (free signup → private beta →
 * waitlist). When the operator flips `signup_mode` to open, restore "Sign up
 * free" here and in the listing-detail upsell — the prod-cutover runbook
 * carries the reminder.
 */
export default function PublicSearchHeader() {
  return (
    <header className='border-b border-border'>
      <div className='mx-auto flex w-full max-w-6xl items-center justify-between gap-4 px-4 py-4 md:px-6'>
        <Link
          href='/'
          aria-label='WyrdFold home'
          className='flex items-center gap-2 rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2 focus-visible:ring-offset-surface'
        >
          <WyrdfoldWordmark aria-hidden className='h-6 w-auto' />
        </Link>
        <div className='flex items-center gap-2'>
          <LinkButton
            name='public-search-sign-in'
            href='/login'
            variant='bare'
            size='sm'
          >
            Sign in
          </LinkButton>
          <LinkButton
            name='public-search-sign-up'
            href='/login'
            variant='primary'
            size='sm'
          >
            Get early access
          </LinkButton>
        </div>
      </div>
    </header>
  );
}
