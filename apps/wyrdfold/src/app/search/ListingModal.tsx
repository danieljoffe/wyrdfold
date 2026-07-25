'use client';

import { useCallback, useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Modal } from '@danieljoffe/shared-ui/Modal';
import { Spinner } from '@danieljoffe/shared-ui/Spinner';
import { Text } from '@danieljoffe/shared-ui/Text';
import Button from '@/components/kit/Button';
import JobDetailModal from './JobDetailModal';
import type { JobSearchResult } from './types';
import { useListingBindings } from './useListingBindings';

/**
 * The intercepted `/search/[id]` detail (#467 §11.2 fast-follow) — the client
 * host the `@modal/(.)[id]` slot renders over the grid on a soft navigation.
 * The card grid stays mounted underneath (interception keeps the previous
 * page as `children`), so closing is `router.back()`: the URL returns to
 * `/search?…` and the slot unwinds to its null default.
 *
 * Fetches the listing itself from the public BFF route (`/api/public/listings`
 * — the cards carry only the row projection, and a modal driven by its OWN
 * fetch behaves identically to the hard-load page, one data path to trust).
 * Membership + the targets picker ride {@link useListingBindings}, so the
 * bind→unlock live flip works exactly as it did when the explorer hosted the
 * modal. States: loading → the modal shell with a spinner; fetch-fail/404 →
 * a small "listing unavailable" body; loaded → the existing
 * {@link JobDetailModal}.
 */
export default function ListingModal({
  id,
  isAuthenticated,
}: {
  id: string;
  isAuthenticated: boolean;
}) {
  const router = useRouter();
  const close = useCallback(() => router.back(), [router]);
  const [job, setJob] = useState<JobSearchResult | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const { inTargets, targetsSource, handleAdded } = useListingBindings(
    id,
    isAuthenticated
  );

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const res = await fetch(
          `/api/public/listings/${encodeURIComponent(id)}`
        );
        if (!res.ok) {
          // 404 (missing/delisted) and transient failures render the same
          // honest "unavailable" state — a modal is no place for a stack trace.
          if (!cancelled) setUnavailable(true);
          return;
        }
        const data = (await res.json()) as JobSearchResult;
        if (!cancelled) setJob(data);
      } catch {
        if (!cancelled) setUnavailable(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [id]);

  if (unavailable) {
    return (
      <Modal isOpen onClose={close} size='md'>
        <div className='flex flex-col gap-3 pr-8'>
          <Text variant='body' as='p' className='font-semibold'>
            This listing is unavailable
          </Text>
          <Text variant='meta' as='p' className='text-text-secondary'>
            It may have been filled or taken down — or the link is off by a
            character.
          </Text>
          <Button
            name='listing-unavailable-close'
            variant='secondary'
            size='sm'
            onClick={close}
            className='self-start'
          >
            Close
          </Button>
        </div>
      </Modal>
    );
  }

  if (!job) {
    return (
      <Modal isOpen onClose={close} size='md'>
        {/* The Spinner itself is the role=status live region. */}
        <div className='flex items-center gap-2'>
          <Spinner size='sm' aria-label='Loading listing' />
          <Text variant='meta'>Loading listing…</Text>
        </div>
      </Modal>
    );
  }

  return (
    <JobDetailModal
      job={job}
      inTargets={inTargets}
      isAuthenticated={isAuthenticated}
      targetsSource={targetsSource}
      onAddedToTarget={handleAdded}
      onClose={close}
    />
  );
}
