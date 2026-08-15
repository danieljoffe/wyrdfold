'use client';

import { useEffect, useState } from 'react';
import { Modal } from '@danieljoffe/shared-ui/Modal';
import { Text } from '@danieljoffe/shared-ui/Text';
import Button from '@/components/kit/Button';
import type { ActiveLimitDetail } from '@/lib/activeLimit';

interface SwapActiveTargetModalProps {
  /** The refused activation's details, or null when the modal is closed. */
  detail: ActiveLimitDetail | null;
  /** Label of the target the user was trying to activate. */
  incomingLabel: string;
  onClose: () => void;
  /** Retry the activation, deactivating `deactivateId` to free the slot. */
  onSwap: (deactivateId: string) => Promise<void>;
}

/**
 * Pick a target to deactivate so another can be activated.
 *
 * Activating past the cap used to be a dead end: a toast saying "deactivate
 * one first" and no way to do it from there. The user had to remember which
 * of their targets were active, navigate to one, deactivate it, navigate
 * back, and activate again.
 *
 * The list is whatever the server sent, so this works unchanged at every tier
 * — one radio on free (cap 1), two on starter, five on pro. Even at cap 1 the
 * choice is shown rather than swapped silently: deactivating someone's only
 * active target is not something to do without naming it.
 */
export default function SwapActiveTargetModal({
  detail,
  incomingLabel,
  onClose,
  onSwap,
}: SwapActiveTargetModalProps) {
  const [selected, setSelected] = useState<string | null>(null);
  const [swapping, setSwapping] = useState(false);

  const choices = detail?.activeTargets ?? [];

  // Preselect when there's only one thing to pick — at cap 1 (the default
  // tier) making the user click the sole radio is friction with no decision
  // behind it. The radio still renders, so what will happen stays visible.
  useEffect(() => {
    if (!detail) {
      setSelected(null);
      setSwapping(false);
      return;
    }
    setSelected(choices.length === 1 ? (choices[0]?.id ?? null) : null);
  }, [detail, choices]);

  if (!detail) return null;

  // The server couldn't name what's active — fall back to its own sentence
  // rather than rendering an empty picker that can't do anything.
  const hasChoices = choices.length > 0;

  return (
    <Modal
      isOpen
      onClose={onClose}
      title={`You're at your limit of ${detail.limit} active ${
        detail.limit === 1 ? 'target' : 'targets'
      }`}
      size='md'
      footer={
        <div className='flex justify-end gap-2'>
          <Button
            name='swap-active-cancel'
            variant='outline'
            size='sm'
            onClick={onClose}
            disabled={swapping}
          >
            Cancel
          </Button>
          {hasChoices && (
            <Button
              name='swap-active-confirm'
              variant='primary'
              size='sm'
              disabled={!selected || swapping}
              onClick={() => {
                if (!selected) return;
                setSwapping(true);
                void onSwap(selected).finally(() => setSwapping(false));
              }}
            >
              {swapping ? 'Swapping…' : 'Swap'}
            </Button>
          )}
        </div>
      }
    >
      {hasChoices ? (
        <div className='flex flex-col gap-3'>
          <Text variant='body' as='p' className='text-text-secondary'>
            Only active targets are matched against new jobs. Choose one to
            deactivate so <strong>{incomingLabel}</strong> can take its place —
            deactivated targets keep their settings and can be switched back any
            time.
          </Text>
          <fieldset className='flex flex-col gap-1'>
            <legend className='sr-only'>Choose a target to deactivate</legend>
            {choices.map(c => (
              <label
                key={c.id}
                className='flex cursor-pointer items-center gap-2 rounded-lg border border-border p-3 hover:bg-surface-secondary'
              >
                <input
                  type='radio'
                  name='swap-out-target'
                  value={c.id}
                  checked={selected === c.id}
                  onChange={() => setSelected(c.id)}
                  disabled={swapping}
                />
                <span className='min-w-0 flex-1 truncate text-sm text-text-primary'>
                  {c.label}
                </span>
              </label>
            ))}
          </fieldset>
        </div>
      ) : (
        <Text variant='body' as='p' className='text-text-secondary'>
          {detail.message}
        </Text>
      )}
    </Modal>
  );
}
