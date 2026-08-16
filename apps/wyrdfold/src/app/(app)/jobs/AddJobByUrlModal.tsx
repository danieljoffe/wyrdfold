'use client';

import { useEffect, useState } from 'react';
import { Modal } from '@danieljoffe/shared-ui/Modal';
import { Input } from '@danieljoffe/shared-ui/Input';
import { Text } from '@danieljoffe/shared-ui/Text';
import Button from '@/components/kit/Button';
import type { AddJobSubmission, ExtractedFields } from './useAddJobByUrl';

interface AddJobByUrlModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSubmit: (input: AddJobSubmission) => Promise<boolean>;
  submitting: boolean;
  /** Why the last attempt failed. Rendered next to the URL field. */
  error: string | null;
  /** API fetched the page but couldn't read a title — collect it by hand. */
  needsManualFields: boolean;
  /** Whatever the API did manage to extract, used to pre-fill the fallback. */
  extracted: ExtractedFields | null;
}

/**
 * Replaces the `window.prompt` this flow used to run on.
 *
 * A native prompt can't validate, can't render an error beside the field, and
 * throws away what the user typed the moment it closes — so a failed add left
 * them with a toast and an empty clipboard. It also blocks the main thread.
 *
 * The second job here is the manual fallback. When the API returns
 * `needs_manual_fields` it is telling us it fetched the page but couldn't find
 * a title; it accepts `title` / `company_name` / `location` overrides on the
 * same endpoint and prefers them over its own extraction. So instead of a dead
 * end we show those three fields, pre-filled with whatever it did find, and
 * re-submit the same URL.
 */
export default function AddJobByUrlModal({
  isOpen,
  onClose,
  onSubmit,
  submitting,
  error,
  needsManualFields,
  extracted,
}: AddJobByUrlModalProps) {
  const [url, setUrl] = useState('');
  const [title, setTitle] = useState('');
  const [companyName, setCompanyName] = useState('');
  const [location, setLocation] = useState('');

  // Reset the URL only when the modal is (re)opened — a failed submit must
  // keep it, that's the whole point of moving off window.prompt.
  useEffect(() => {
    if (isOpen) {
      setUrl('');
      setTitle('');
      setCompanyName('');
      setLocation('');
    }
  }, [isOpen]);

  // Pre-fill the fallback with the partial extraction the moment it arrives.
  useEffect(() => {
    if (!needsManualFields || !extracted) return;
    setTitle(prev => prev || extracted.title || '');
    setCompanyName(prev => prev || extracted.company_name || '');
    setLocation(prev => prev || extracted.location || '');
  }, [needsManualFields, extracted]);

  const handleSubmit = () => {
    void onSubmit({
      url,
      ...(needsManualFields
        ? {
            title: title.trim(),
            company_name: companyName.trim(),
            location: location.trim(),
          }
        : {}),
    });
  };

  return (
    <Modal isOpen={isOpen} onClose={onClose} title='Add a job by URL'>
      <div className='flex flex-col gap-4'>
        <Input
          label='Job posting URL'
          type='url'
          value={url}
          onChange={e => setUrl(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter' && !submitting) handleSubmit();
          }}
          placeholder='https://job-boards.greenhouse.io/acme/jobs/123456'
          error={error ?? undefined}
          autoFocus
        />

        {needsManualFields && (
          <div className='flex flex-col gap-3'>
            <Text variant='caption' className='text-text-secondary'>
              We reached the page but couldn&apos;t read the posting details.
              Fill these in and we&apos;ll add it with the URL above.
            </Text>
            <Input
              label='Job title'
              required
              value={title}
              onChange={e => setTitle(e.target.value)}
              placeholder='Senior Frontend Engineer'
            />
            <Input
              label='Company'
              value={companyName}
              onChange={e => setCompanyName(e.target.value)}
              placeholder='Acme'
            />
            <Input
              label='Location'
              value={location}
              onChange={e => setLocation(e.target.value)}
              placeholder='Remote (US)'
            />
          </div>
        )}

        <div className='flex justify-end gap-2'>
          <Button
            name='add-job-cancel'
            variant='outline'
            size='sm'
            onClick={onClose}
            disabled={submitting}
          >
            Cancel
          </Button>
          <Button
            name='add-job-submit'
            variant='primary'
            size='sm'
            onClick={handleSubmit}
            disabled={
              submitting || !url.trim() || (needsManualFields && !title.trim())
            }
          >
            {submitting ? 'Adding...' : 'Add job'}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
