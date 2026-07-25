import Link from 'next/link';

import LinkButton from '@/components/kit/LinkButton';
import WyrdfoldWordmark from '@/components/WyrdfoldWordmark';

/**
 * Lean public header for the logged-out `/search` surface (#467 §10). Not the
 * full marketing shell (`(public)/layout.tsx`) and not the app sidebar — just a
 * wordmark home-link and one calm "Sign up free" conversion path. The stronger,
 * single conversion moment lives in the listing detail (§11.5); the header stays
 * quiet — a "Sign in" ghost + the "Sign up free" CTA, both to `/login` (signup
 * lives behind the flag; `/login` is the one door for both).
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
            Sign up free
          </LinkButton>
        </div>
      </div>
    </header>
  );
}
