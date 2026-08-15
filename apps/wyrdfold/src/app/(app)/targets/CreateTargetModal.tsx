'use client';

import { useCallback, useEffect, useState } from 'react';
import { Modal } from '@danieljoffe/shared-ui/Modal';
import { Input } from '@danieljoffe/shared-ui/Input';
import { Textarea } from '@danieljoffe/shared-ui/Textarea';
import { Tabs, type Tab } from '@danieljoffe/shared-ui/Tabs';
import { Text } from '@danieljoffe/shared-ui/Text';
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
  /**
   * Why the last submit failed. Rendered inline next to the field that caused
   * it, and shown only on that tab — see the alert below for why the toast
   * alone was not enough.
   */
  error?: string | undefined;
}

export default function CreateTargetModal({
  isOpen,
  onClose,
  onSubmitManual,
  onSubmitUrl,
  onFollow,
  onCreateSuggestion,
  draft,
  error,
}: CreateTargetModalProps) {
  // Discovery-first: default to searching the shared catalog so a user follows
  // an existing target instead of minting a duplicate; Manual / From URL are
  // the create fallbacks.
  const [mode, setMode] = useState<Mode>('search');
  const [label, setLabel] = useState('');
  const [description, setDescription] = useState('');
  const [jdUrl, setJdUrl] = useState('');

  // `Tabs` is uncontrolled, so the only way to move the user to another tab is
  // to remount it with a new `defaultTab`. Both jumps below need that: a
  // restored draft (land back where you failed) and "create this manually"
  // out of an empty search.
  const [tabsKey, setTabsKey] = useState(0);
  const [initialTab, setInitialTab] = useState<Mode>('search');

  const jumpTo = useCallback((next: Mode) => {
    setMode(next);
    setInitialTab(next);
    setTabsKey(k => k + 1);
  }, []);

  // Re-open holding the failed draft. Keyed on `draft` identity: the parent
  // hands over a fresh object per failure, so a second failure re-seeds even
  // if the user had edited the field in between.
  useEffect(() => {
    if (!isOpen || !draft) return;
    setLabel(draft.label ?? '');
    setDescription(draft.description ?? '');
    setJdUrl(draft.jdUrl ?? '');
    jumpTo(draft.mode);
  }, [isOpen, draft, jumpTo]);

  const reset = useCallback(() => {
    setLabel('');
    setDescription('');
    setJdUrl('');
    setMode('search');
    setInitialTab('search');
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
          onCreateManually={q => {
            setLabel(q);
            jumpTo('manual');
          }}
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

  // The error belongs to the tab that produced it — an extraction failure has
  // nothing to say while the user is looking at Manual.
  const showError =
    Boolean(error) && draft !== undefined && mode === draft.mode;

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
            {/* Search applies each Follow immediately, so there is no draft to
                abandon and "Cancel" read as "undo what I just did" right after
                a successful follow. */}
            {mode === 'search' ? 'Done' : 'Cancel'}
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
      {/* A failed create was announced ONLY by a toast, which auto-dismisses --
          while this modal stayed open holding the draft and showing nothing.
          The from-url fetch runs 10-20s, so the user who looks away comes back
          to a modal that looks untouched. Same defect #742 fixed for the
          suggest actions; the create path kept it. */}
      {showError && (
        <div
          role='alert'
          className='mb-1 rounded-md border border-error/30 bg-error-light p-3'
        >
          <Text variant='meta' as='p' className='text-error'>
            {error}
          </Text>
        </div>
      )}

      <Tabs
        key={tabsKey}
        tabs={tabs}
        defaultTab={initialTab}
        variant='underline'
        onChange={id => setMode(id as Mode)}
      />
    </Modal>
  );
}
