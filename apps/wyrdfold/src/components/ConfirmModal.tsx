'use client';

import type { ReactNode } from 'react';
import { Modal } from '@danieljoffe/shared-ui/Modal';
import { Spinner } from '@danieljoffe/shared-ui/Spinner';
import { Text } from '@danieljoffe/shared-ui/Text';
import Button from '@/components/Button';

interface ConfirmModalProps {
  isOpen: boolean;
  onClose: () => void;
  onConfirm: () => void | Promise<void>;
  title: string;
  /** Body copy. A string is wrapped in <Text>; pass a node for richer content. */
  message?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  /** Style the confirm button as destructive (red). */
  destructive?: boolean;
  /** Disable both buttons + show a spinner on confirm while the action runs. */
  loading?: boolean;
  /** Label shown beside the spinner while loading (e.g. "Deleting…"). */
  loadingLabel?: string;
  /**
   * Disable the confirm button only (cancel stays enabled). Use to gate an
   * irreversible action behind a typed confirmation — the caller renders the
   * input in ``message`` and passes ``confirmDisabled`` until it matches.
   */
  confirmDisabled?: boolean;
  /** Base name for the rendered buttons (stable a11y/test hooks). */
  name?: string;
}

/**
 * App-styled confirmation dialog — a thin compose over shared-ui `Modal`.
 *
 * Replaces native `window.confirm()` so destructive / irreversible actions
 * match the app's design system, don't block the main thread synchronously,
 * and can surface an in-flight state on the confirming action (disable +
 * spinner) instead of leaving the user staring at a frozen browser popup.
 */
export default function ConfirmModal({
  isOpen,
  onClose,
  onConfirm,
  title,
  message,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  destructive = false,
  loading = false,
  loadingLabel,
  confirmDisabled = false,
  name = 'confirm-modal',
}: ConfirmModalProps) {
  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title={title}
      size='sm'
      closeOnBackdropClick={!loading}
      footer={
        <div className='flex justify-end gap-2'>
          <Button
            name={`${name}-cancel`}
            variant='outline'
            size='sm'
            onClick={onClose}
            disabled={loading}
          >
            {cancelLabel}
          </Button>
          <Button
            name={`${name}-confirm`}
            variant={destructive ? 'error' : 'primary'}
            size='sm'
            onClick={() => void onConfirm()}
            disabled={loading || confirmDisabled}
            aria-busy={loading}
          >
            {loading ? (
              <>
                <Spinner size='sm' aria-hidden />
                <span>{loadingLabel ?? `${confirmLabel}…`}</span>
              </>
            ) : (
              confirmLabel
            )}
          </Button>
        </div>
      }
    >
      {typeof message === 'string' ? (
        <Text as='p' variant='body'>
          {message}
        </Text>
      ) : (
        message
      )}
    </Modal>
  );
}
