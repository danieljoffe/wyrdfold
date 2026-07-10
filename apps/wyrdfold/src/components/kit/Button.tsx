'use client';

import { Button as SharedButton } from '@danieljoffe/shared-ui/Button';
import type { ButtonProps } from '@danieljoffe/shared-ui/Button';
import { cn } from '@/lib/cn';

/**
 * Thin styling shim over shared-ui's Button — API-identical (variant, size,
 * loading, iconOnly, as/href all pass straight through). Exists only to
 * re-apply two app-validated behaviors the library doesn't ship (yet):
 *
 * 1. Neutral disabled treatment. The lib uses `disabled:opacity-50`;
 *    semi-transparent chartreuse on pyre's primary reads as a
 *    "rejected/error" state to first-time users (Phase 4a finding #7).
 *    Neutral surface + tertiary text reads as "fill the form first".
 * 2. WCAG 2.5.5 minimum 44×44 hit area for icon-only buttons (#25 F2) —
 *    the lib's iconOnly paddings can produce sub-44px targets with sm icons.
 *
 * Both are upstream candidates for shared-ui; when they land this file
 * becomes a pure re-export and can be codemodded away. Per the shared-ui
 * README ("Extend, Don't Reimplement"), the shim composes via className —
 * `cn` (tailwind-merge) lets the later disabled:* utilities win over the
 * lib's.
 */
const NEUTRAL_DISABLED =
  'disabled:opacity-100 disabled:bg-surface-elevated disabled:text-text-tertiary ' +
  'disabled:border disabled:border-border disabled:shadow-none ' +
  'disabled:hover:bg-surface-elevated disabled:hover:scale-100 disabled:hover:shadow-none';

export type { ButtonProps };

export default function Button({ className, ...props }: ButtonProps) {
  return (
    <SharedButton
      {...props}
      className={cn(
        NEUTRAL_DISABLED,
        props.iconOnly && 'min-h-11 min-w-11',
        className
      )}
    />
  );
}
