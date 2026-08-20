'use client';

import Button from '@/components/kit/Button';
import AddJobByUrlModal from './AddJobByUrlModal';
import { useAddJobByUrl } from './useAddJobByUrl';

interface AddJobByUrlButtonProps {
  /** Refresh the list after a posting is actually created. */
  onJobAdded: () => void;
  label?: string;
  variant?: 'outline' | 'primary';
  size?: 'sm' | 'md';
  /** Stable a11y/test hook — call sites render this in three places. */
  name: string;
}

/**
 * Button + modal for "add a job by pasting its URL", packaged together so the
 * three surfaces that offer it (empty state, thin-results callout, list
 * toolbar) don't each have to own hook state and render the dialog.
 *
 * The toolbar mount is the point of this component existing: the flow used to
 * be reachable *only* from the empty state (0 jobs) and the thin callout (1–4),
 * so "I found a job elsewhere, add it and tailor a resume" became impossible
 * the moment the product started working and the list filled up.
 */
export default function AddJobByUrlButton({
  onJobAdded,
  label = 'Add job by URL',
  variant = 'outline',
  size = 'sm',
  name,
}: AddJobByUrlButtonProps) {
  const {
    isOpen,
    open,
    close,
    submit,
    submitting,
    error,
    needsManualFields,
    extracted,
  } = useAddJobByUrl(onJobAdded);

  return (
    <>
      <Button name={name} variant={variant} size={size} onClick={open}>
        {label}
      </Button>
      <AddJobByUrlModal
        isOpen={isOpen}
        onClose={close}
        onSubmit={submit}
        submitting={submitting}
        error={error}
        needsManualFields={needsManualFields}
        extracted={extracted}
      />
    </>
  );
}
