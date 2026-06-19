import type { ComponentProps, ReactNode } from 'react';
import { Badge } from '@danieljoffe/shared-ui/Badge';
import { cn } from '@/lib/cn';

export type CircleBadgeVariant = NonNullable<
  ComponentProps<typeof Badge>['variant']
>;
export type CircleBadgeSize = NonNullable<ComponentProps<typeof Badge>['size']>;

// Fixed square dimensions per size so `rounded-full` yields a true circle and
// comfortably fits up to ~3 characters with tabular-nums.
const SIZE_CLASS: Record<CircleBadgeSize, string> = {
  sm: 'size-7 text-[11px]',
  md: 'size-9 text-xs',
  lg: 'size-11 text-sm',
};

interface CircleBadgeProps {
  children: ReactNode;
  variant?: CircleBadgeVariant;
  size?: CircleBadgeSize;
  title?: string | undefined;
  ariaLabel?: string | undefined;
  className?: string | undefined;
}

/**
 * Circular chip — the shared-ui `Badge` pill forced to a true circle.
 *
 * `Badge` is a content-width pill (`rounded-md` + horizontal padding); these
 * classes override that via tailwind-merge (fixed square + `rounded-full` +
 * centred + no horizontal padding) while keeping the variant colours. Use for
 * compact numeric indicators — match/fit scores, weights, percentages, counts.
 */
export default function CircleBadge({
  children,
  variant = 'default',
  size = 'md',
  title,
  ariaLabel,
  className,
}: CircleBadgeProps) {
  return (
    <Badge
      variant={variant}
      size={size}
      title={title}
      aria-label={ariaLabel}
      className={cn(
        'aspect-square justify-center rounded-full p-0 tabular-nums',
        SIZE_CLASS[size],
        className
      )}
    >
      {children}
    </Badge>
  );
}
