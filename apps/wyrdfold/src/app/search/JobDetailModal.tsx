'use client';

import { Modal } from '@danieljoffe/shared-ui/Modal';
import ListingDetailBody from './ListingDetailBody';
import type { JobSearchResult, TargetRef } from './types';
import type { TargetsSource } from './useTargetsSource';

interface JobDetailModalProps {
  job: JobSearchResult;
  /** The caller's targets that already contain this listing (empty = unbound). */
  inTargets: TargetRef[];
  /** Logged-out: the detail shows info + source link + ONE soft signup allusion
   *  (#467 §11.5) instead of the bind / LLM actions. Authed: unchanged. */
  isAuthenticated: boolean;
  targetsSource: TargetsSource;
  onClose: () => void;
  /** Fired when the user binds this listing to a target from inside the modal,
   *  so the host can reflect it live — flipping this modal to its bound state
   *  (match/tailor unlock) (#467 §11.3). */
  onAddedToTarget?: ((target: TargetRef) => void) | undefined;
}

/**
 * The listing detail as a MODAL (#467 §11.2) — the shared-ui `Modal` frame
 * (role=dialog, aria-modal, Esc + backdrop close, focus trap + restore, scroll
 * lock) around {@link ListingDetailBody}, which owns all the content and the
 * contextual actions. The body is a separate component because the detail is
 * URL-addressable (§11.2 fast-follow): the intercepted `/search/[id]` route
 * renders THIS modal over the grid, while a hard load renders the same body as
 * a full page. No `title` prop is passed because the header is a custom
 * composition (monogram · role · company·location · source link); the Modal
 * renders its own ✕ in that mode.
 */
export default function JobDetailModal({
  job,
  inTargets,
  isAuthenticated,
  targetsSource,
  onClose,
  onAddedToTarget,
}: JobDetailModalProps) {
  return (
    <Modal isOpen onClose={onClose} size='md'>
      <ListingDetailBody
        job={job}
        inTargets={inTargets}
        isAuthenticated={isAuthenticated}
        targetsSource={targetsSource}
        onAddedToTarget={onAddedToTarget}
      />
    </Modal>
  );
}
