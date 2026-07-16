'use client';

import { useCallback, useState } from 'react';
import { Modal } from '@danieljoffe/shared-ui/Modal';
import { Input } from '@danieljoffe/shared-ui/Input';
import { Textarea } from '@danieljoffe/shared-ui/Textarea';
import { Tabs, type Tab } from '@danieljoffe/shared-ui/Tabs';
import Button from '@/components/kit/Button';
import TargetSearchTab from './TargetSearchTab';
import type { TargetSearchResult } from './types';

export interface ManualSubmission {
  label: string;
  description: string | undefined;
}

export interface UrlSubmission {
  jd_url: string;
  label: string | undefined;
}

type Mode = 'search' | 'manual' | 'url';

interface CreateTargetModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSubmitManual: (payload: ManualSubmission) => void;
  onSubmitUrl: (payload: UrlSubmission) => void;
  onFollow: (target: TargetSearchResult) => Promise<boolean>;
}

export default function CreateTargetModal({
  isOpen,
  onClose,
  onSubmitManual,
  onSubmitUrl,
  onFollow,
}: CreateTargetModalProps) {
  // Discovery-first: default to searching the shared catalog so a user follows
  // an existing target instead of minting a duplicate; Manual / From URL are
  // the create fallbacks.
  const [mode, setMode] = useState<Mode>('search');
  const [label, setLabel] = useState('');
  const [description, setDescription] = useState('');
  const [urlLabel, setUrlLabel] = useState('');
  const [jdUrl, setJdUrl] = useState('');

  const reset = useCallback(() => {
    setLabel('');
    setDescription('');
    setUrlLabel('');
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
      const trimmedLabel = urlLabel.trim();
      onSubmitUrl({
        jd_url: trimmedUrl,
        label: trimmedLabel || undefined,
      });
    }
    reset();
  }, [
    mode,
    label,
    description,
    urlLabel,
    jdUrl,
    onSubmitManual,
    onSubmitUrl,
    reset,
  ]);

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
      content: <TargetSearchTab onFollow={onFollow} />,
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
            helperText='A short note about this role. The LLM will use it (alongside your experience) to canonicalize the target and derive a scoring profile.'
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
            helperText="We'll fetch the page and derive a scoring profile from the job description."
            placeholder='https://...'
            value={jdUrl}
            onChange={e => setJdUrl(e.target.value)}
            type='url'
          />
          <Input
            label='Title (optional)'
            helperText='Leave blank to use the role title from the job posting.'
            placeholder='e.g. Senior Frontend Engineer'
            value={urlLabel}
            onChange={e => setUrlLabel(e.target.value)}
            maxLength={200}
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
      <Tabs
        tabs={tabs}
        defaultTab='search'
        variant='underline'
        onChange={id => setMode(id as Mode)}
      />
    </Modal>
  );
}
