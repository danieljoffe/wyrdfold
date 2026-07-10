'use client';

import { useCallback, type ComponentProps, type ReactNode } from 'react';
import Link from 'next/link';
import type { Url } from 'next/dist/shared/lib/router/router';
import { useRouter } from 'next/navigation';
import {
  baseButtonStyles,
  sizeButtonStyles,
  variantButtonStyles,
} from '@danieljoffe/shared-ui/Button';
import type { ButtonSize, ButtonVariant } from '@danieljoffe/shared-ui/Button';
import { cn } from '@/lib/cn';

interface LinkButtonProps extends Omit<
  ComponentProps<typeof Link>,
  'children'
> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Renders a non-interactive, link-styled span (links have no native
   *  disabled state). */
  disabled?: boolean;
  /** Analytics/test hook — every app button carries one; passes through to
   *  the anchor as a plain attribute (not in React's AnchorHTMLAttributes). */
  name?: string;
  children: ReactNode;
}

/**
 * A next/link styled as a shared-ui Button. Lives in `components/kit/` per
 * the shared-ui README constraint — the library is framework-agnostic, so
 * Next.js-coupled composition belongs in the consuming app. Composes the
 * library's exported style records (base/variant/size) rather than copying
 * classes, so a library restyle flows through automatically.
 *
 * Ported behaviors from the bespoke Button's link mode:
 * - Prefetch-on-hover for internal routes (snappier nav than viewport-only
 *   prefetch on long lists).
 * - `disabled` renders an aria-disabled span — links have no disabled state.
 * - `target='_blank'` auto-applies `rel='noopener noreferrer'`.
 * - Falls back to an id derived from `aria-label` (test/analytics hooks).
 */
export default function LinkButton({
  variant = 'primary',
  size = 'md',
  disabled,
  children,
  className,
  ...rest
}: LinkButtonProps) {
  const router = useRouter();

  const classes = cn(
    baseButtonStyles,
    variantButtonStyles[variant],
    sizeButtonStyles[size],
    disabled && 'pointer-events-none',
    className
  );

  const handleMouseEnter = useCallback(
    (href: Url) => {
      if (href && href.toString().startsWith('/')) {
        router.prefetch(href.toString());
      }
    },
    [router]
  );

  if (rest.href == null || rest.href.toString().length === 0) return null;

  if (disabled) {
    return (
      <span className={classes} aria-disabled={true} role='link' tabIndex={-1}>
        {children}
      </span>
    );
  }

  const { href, id, target, 'aria-label': ariaLabel } = rest;
  const rel = target === '_blank' ? 'noopener noreferrer' : undefined;

  return (
    <Link
      {...rest}
      className={classes}
      onMouseEnter={() => handleMouseEnter(href)}
      id={id ?? ariaLabel?.replace(' ', '-')}
      href={href}
      aria-label={ariaLabel}
      rel={rel}
    >
      {children}
    </Link>
  );
}
