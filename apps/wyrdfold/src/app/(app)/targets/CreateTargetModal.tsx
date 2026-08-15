'use client';

import { useCallback, useEffect, useState } from 'react';
import { Modal } from '@danieljoffe/shared-ui/Modal';
import { Input } from '@danieljoffe/shared-ui/Input';
import { Textarea } from '@danieljoffe/shared-ui/Textarea';
import { Tabs, type Tab } from '@danieljoffe/shared-ui/Tabs';
import Button from '@/components/kit/Button';
import TargetSearchTab from './TargetSearchTab';
import type { MatchedSuggestion, TargetSearchResult } from './types';

export interface ManualSubmission {
  label: string;
  description: string | undefined;
}

export interface UrlSubmission {
  jd_url: string;
}

type Mode = 'search' | 'manual' | 'url';

/**
 * What the user had typed when a create failed, so the modal can come back
 * holding it. Creation closes the modal optimistically (the happy path is the
 * common one), which meant a failure dropped the typed URL or title on the
 * floor — for `from-url` in particular, the error tells you to try something
 * else, and you no longer have the thing you typed to try it with.
 */
export interface CreateTargetDraft {
  mode: Exclude<Mode, 'search'>;
  label?: string;
  description?: string;
  jdUrl?: string;
}

interface CreateTargetModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSubmitManual: (payload: ManualSubmission) => void;
  onSubmitUrl: (payload: UrlSubmission) => void;
  onFollow: (target: TargetSearchResult) => Promise<boolean>;
  onCreateSuggestion: (match: MatchedSuggestion) => Promise<boolean>;
  /** Restored on open after a failed submit; cleared by the parent. */
  draft?: CreateTargetDraft | undefined;
}

export default function CreateTargetModal({
  isOpen,
  onClose,
  onSubmitManual,
  onSubmitUrl,
  onFollow,
  onCreateSuggestion,
  draft,
}: CreateTargetModalProps) {
  // Discovery-first: default to searching the shared catalog so a user follows
  // an existing target instead of minting a duplicate; Manual / From URL are
  // the create fallbacks.
  const [mode, setMode] = useState<Mode>('search');
  const [label, setLabel] = useState('');
  const [description, setDescription] = useState('');
  const [jdUrl, setJdUrl] = useState('');

  // Re-open holding the failed draft. Keyed on `draft` identity: the parent
  // hands over a fresh object per failure, so a second failure re-seeds even
  // if the user had edited the field in between.
  useEffect(() => {
    if (!isOpen || !draft) return;
    setMode(draft.mode);
    setLabel(draft.label ?? '');
    setDescription(draft.description ?? '');
    setJdUrl(draft.jdUrl ?? '');
  }, [isOpen, draft]);

  // Bumped per restored draft so the uncontrolled Tabs remounts (see below).
  const [draftKey, setDraftKey] = useState(0);
  useEffect(() => {
    if (isOpen && draft) setDraftKey(k => k + 1);
  }, [isOpen, draft]);

  const reset = useCallback(() => {
    setLabel('');
    setDescription('');
    setJdUrl('');
    setMode('search');
  }, []);

  const handleClose = useCallback(() => {
    reset();
    onClose();
  }, [reset, onClose]);

  const handleSubmit = useCallback(() => {
    if (mode === 'manual') {
      const trimmedLabel = label.trim();
      if (!trimmedLabel) return;
      const trimmedDescription = description.trim();
      onSubmitManual({
        label: trimmedLabel,
        description: trimmedDescription || undefined,
      });
    } else {
      const trimmedUrl = jdUrl.trim();
      if (!trimmedUrl) return;
      // No user-supplied title — the label is always derived from the posting.
      onSubmitUrl({ jd_url: trimmedUrl });
    }
    // Deliberately NOT reset here: the parent closes the modal optimistically
    // and, on failure, re-opens it seeded with this draft. Wiping now would
    // race that restore and hand the user an empty form after an error.
  }, [mode, label, description, jdUrl, onSubmitManual, onSubmitUrl]);

  const canSubmit =
    mode === 'manual'
      ? label.trim().length > 0
      : mode === 'url'
        ? jdUrl.trim().length > 0
        : false;

  const tabs: Tab[] = [
    {
      id: 'search',
      label: 'Search',
      content: (
        <TargetSearchTab
          onFollow={onFollow}
          onCreateSuggestion={onCreateSuggestion}
        />
      ),
    },
    {
      id: 'manual',
      label: 'Manual',
      content: (
        <div className='flex flex-col gap-4 pt-4'>
          <Input
            label='Title'
            placeholder='e.g. Senior Frontend Engineer'
            value={label}
            onChange={e => setLabel(e.target.value)}
            maxLength={200}
          />
          <Textarea
            label='Description (optional)'
            helperText='A short note about this role — used alongside your experience to name the target and build its scoring profile.'
            placeholder='Roles I want to optimize for...'
            value={description}
            onChange={e => setDescription(e.target.value)}
            rows={4}
          />
        </div>
      ),
    },
    {
      id: 'url',
      label: 'From URL',
      content: (
        <div className='flex flex-col gap-4 pt-4'>
          <Input
            label='Job description URL'
            helperText="We'll fetch the page, derive a scoring profile, and add the posting as a saved job — the title comes from the posting itself."
            placeholder='https://...'
            value={jdUrl}
            onChange={e => setJdUrl(e.target.value)}
            type='url'
          />
        </div>
      ),
    },
  ];

  return (
    <Modal
      isOpen={isOpen}
      onClose={handleClose}
      title='New Target'
      size='lg'
      footer={
        <div className='flex justify-end gap-2'>
          <Button
            name='target-create-cancel'
            variant='outline'
            size='sm'
            onClick={handleClose}
          >
            Cancel
          </Button>
          {mode !== 'search' && (
            <Button
              name='target-create-submit'
              variant='primary'
              size='sm'
              onClick={handleSubmit}
              disabled={!canSubmit}
            >
              Create Target
            </Button>
          )}
        </div>
      }
    >
      {/* Tabs is uncontrolled, so restoring `mode` alone would leave the user
          looking at the Search tab while the state said "url". Remounting on
          the draft's identity makes `defaultTab` re-apply, landing them back
          on the tab they failed from. */}
      <Tabs
        key={draftKey}
        tabs={tabs}
        defaultTab={draft ? draft.mode : 'search'}
        variant='underline'
        onChange={id => setMode(id as Mode)}
      />
    </Modal>
  );
}
