import type { ComponentType, ReactNode, SVGProps } from 'react';
import Link from 'next/link';
import { Mail } from 'lucide-react';
import Button from '@/components/Button';
import WyrdfoldLogo from '@/components/WyrdfoldLogo';

// lucide-react 1.x removed its deprecated brand icons; these are the same
// stroke paths the 0.x Github/Linkedin icons used, inlined.
function GithubIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox='0 0 24 24'
      fill='none'
      stroke='currentColor'
      strokeWidth='2'
      strokeLinecap='round'
      strokeLinejoin='round'
      {...props}
    >
      <path d='M15 22v-4a4.8 4.8 0 0 0-1-3.5c3 0 6-2 6-5.5.08-1.25-.27-2.48-1-3.5.28-1.15.28-2.35 0-3.5 0 0-1 0-3 1.5-2.64-.5-5.36-.5-8 0C6 2 5 2 5 2c-.3 1.15-.3 2.35 0 3.5A5.403 5.403 0 0 0 4 9c0 3.5 3 5.5 6 5.5-.39.49-.68 1.05-.85 1.65-.17.6-.22 1.23-.15 1.85v4' />
      <path d='M9 18c-4.51 2-5-2-7-2' />
    </svg>
  );
}

function LinkedinIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox='0 0 24 24'
      fill='none'
      stroke='currentColor'
      strokeWidth='2'
      strokeLinecap='round'
      strokeLinejoin='round'
      {...props}
    >
      <path d='M16 8a6 6 0 0 1 6 6v7h-4v-7a2 2 0 0 0-2-2 2 2 0 0 0-2 2v7h-4V8z' />
      <rect width='4' height='12' x='2' y='9' />
      <circle cx='4' cy='4' r='2' />
    </svg>
  );
}

interface SocialLink {
  label: string;
  href: string;
  icon: ComponentType<SVGProps<SVGSVGElement>>;
  external: boolean;
}

const SOCIAL_LINKS: SocialLink[] = [
  {
    label: 'GitHub',
    href: 'https://github.com/danieljoffe',
    icon: GithubIcon,
    external: true,
  },
  {
    label: 'LinkedIn',
    href: 'https://www.linkedin.com/in/daniel-joffe-work',
    icon: LinkedinIcon,
    external: true,
  },
  {
    label: 'Email',
    href: 'mailto:hello@danieljoffe.com',
    icon: Mail,
    external: false,
  },
];

const PORTFOLIO_URL = 'https://danieljoffe.com';

/**
 * Public marketing shell — no sidebar, no mobile nav, no auth required.
 * Auth gating for `/` (the only route currently in this group) is handled
 * upstream in proxy.ts, which redirects signed-in users to /dashboard.
 */
export default function PublicLayout({ children }: { children: ReactNode }) {
  // The page is force-static, so this resolves at build time. Re-deploys
  // pick up the new year; not worth more than that.
  const year = new Date().getFullYear();

  return (
    <div className='min-h-screen flex flex-col bg-surface text-text-primary'>
      {/* Beta strip — signals the app is pre-launch and data is not durable.
          Public-pages-only on purpose: the (app) shell repeats this in the
          dashboard nav so signed-in users don't lose the warning. */}
      <div
        role='status'
        aria-live='polite'
        className='border-b border-border bg-surface-elevated'
      >
        <div className='mx-auto w-full max-w-6xl px-4 py-2 md:px-6'>
          <p className='text-xs md:text-sm text-text-secondary'>
            <span className='font-semibold text-text-primary'>
              Private beta.
            </span>{' '}
            WyrdFold is under active development — accounts and data may be
            reset without notice while we iterate.
          </p>
        </div>
      </div>
      <header className='border-b border-border'>
        <div className='mx-auto flex w-full max-w-6xl items-center justify-between px-4 py-4 md:px-6 md:py-5'>
          <Link
            href='/'
            aria-label='WyrdFold home'
            className='flex items-center gap-2 rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2 focus-visible:ring-offset-surface'
          >
            <WyrdfoldLogo aria-label='WyrdFold' className='h-5 w-6' />
            <span className='text-base text-text-tertiary uppercase tracking-[6px]'>
              WyrdFold
            </span>
            <span
              className='ml-1 inline-flex items-center rounded-full border border-brand-300/40 bg-brand-300/10 px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider text-brand-950 dark:text-brand-300'
              aria-label='Private beta'
            >
              Beta
            </span>
          </Link>
          <Button
            name='wyrdfold-public-header-sign-in'
            as='link'
            href='/login'
            variant='outline'
            size='sm'
          >
            Sign in
          </Button>
        </div>
      </header>
      <main id='main-content' className='flex-1'>
        {children}
      </main>
      <footer className='border-t border-border'>
        <div className='mx-auto flex w-full max-w-6xl flex-col gap-4 px-4 py-8 text-sm text-text-tertiary md:flex-row md:items-center md:justify-between md:gap-6 md:px-6 md:py-10'>
          <p>
            Built by{' '}
            <a
              href={PORTFOLIO_URL}
              target='_blank'
              rel='noopener noreferrer'
              className='rounded-sm text-text-secondary underline underline-offset-2 transition-colors hover:text-text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2 focus-visible:ring-offset-surface'
            >
              Daniel Joffe
            </a>{' '}
            <span aria-hidden='true' className='mx-1 text-text-tertiary'>
              ·
            </span>{' '}
            © {year}
          </p>
          <ul className='flex flex-wrap items-center gap-x-5 gap-y-2'>
            {SOCIAL_LINKS.map(({ label, href, icon: Icon, external }) => (
              <li key={label}>
                <a
                  href={href}
                  {...(external && {
                    target: '_blank',
                    rel: 'noopener noreferrer',
                  })}
                  className='inline-flex items-center gap-1.5 rounded-sm text-text-tertiary transition-colors hover:text-text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2 focus-visible:ring-offset-surface'
                >
                  <Icon className='size-4' aria-hidden='true' />
                  <span>{label}</span>
                </a>
              </li>
            ))}
          </ul>
        </div>
      </footer>
    </div>
  );
}
