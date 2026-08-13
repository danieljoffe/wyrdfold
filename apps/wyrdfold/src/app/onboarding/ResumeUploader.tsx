'use client';

import { useState, useCallback, useRef, useEffect } from 'react';
import { Upload, FileText, CheckCircle } from 'lucide-react';
import { Card } from '@danieljoffe/shared-ui/Card';
import { Text } from '@danieljoffe/shared-ui/Text';
import { Heading } from '@danieljoffe/shared-ui/Heading';
import { Spinner } from '@danieljoffe/shared-ui/Spinner';
import { Alert } from '@danieljoffe/shared-ui/Alert';
import Button from '@/components/kit/Button';
import { cn } from '@/lib/cn';
import { extractApiError } from '@/lib/extractApiError';
import { useStagedMessage } from './useStagedMessage';

const ACCEPTED_TYPES = [
  'application/pdf',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
];
const MAX_FILE_SIZE = 10 * 1024 * 1024; // 10 MB

const PARSE_STAGES = [
  'Uploading and parsing your resume...',
  'Reading your work history...',
  'Building your experience document — this can take up to a minute...',
] as const;

interface ResumeUploaderProps {
  onComplete: () => void;
  onSkip: () => void;
}

export default function ResumeUploader({
  onComplete,
  onSkip,
}: ResumeUploaderProps) {
  const [dragOver, setDragOver] = useState(false);
  const [uploading, setUploading] = useState(false);
  // Parsing a long resume takes ~45s (prod, 2026-08-13); a static line
  // reads as a hang well before that.
  const parseStage = useStagedMessage(PARSE_STAGES, uploading);
  const [error, setError] = useState<string | null>(null);
  const [uploaded, setUploaded] = useState(false);
  // Re-collection dedup (UX/IA §F): a redo-onboarding user already has a
  // parsed source file — offer keep-or-replace instead of silently asking
  // them to upload again (keep = the common case; replace stays one click
  // away for the "my resume changed" redo).
  const [hasExistingDoc, setHasExistingDoc] = useState(false);
  const [replacing, setReplacing] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  useEffect(() => {
    let cancelled = false;
    async function checkExisting() {
      try {
        const res = await fetch('/api/career/experience/optimized');
        if (cancelled || !res.ok) return;
        const body = (await res.json()) as { id?: string };
        // The endpoint returns the doc or `{ optimized: null }` when empty.
        if (!cancelled && typeof body.id === 'string') {
          setHasExistingDoc(true);
        }
      } catch {
        // No fast-path — the drop zone renders as usual.
      }
    }
    checkExisting();
    return () => {
      cancelled = true;
    };
  }, []);

  // Cleanup timeout on unmount
  useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  const upload = useCallback(
    async (file: File) => {
      if (!ACCEPTED_TYPES.includes(file.type)) {
        setError('Please upload a PDF or DOCX file.');
        return;
      }
      if (file.size > MAX_FILE_SIZE) {
        setError('File must be under 10 MB.');
        return;
      }

      setError(null);
      setUploading(true);

      try {
        const formData = new FormData();
        formData.append('file', file);

        const res = await fetch(
          '/api/career/experience/upload-resume?auto_derive=true',
          { method: 'POST', body: formData }
        );

        if (!res.ok) {
          throw new Error(await extractApiError(res, 'Upload failed'));
        }

        setUploaded(true);
        // Brief delay to show success state before advancing
        timerRef.current = setTimeout(onComplete, 1200);
      } catch (err) {
        setError(
          err instanceof Error
            ? err.message
            : 'Upload failed. Please try again.'
        );
      } finally {
        setUploading(false);
      }
    },
    [onComplete]
  );

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setDragOver(false);
      const file = e.dataTransfer.files[0];
      if (file) upload(file);
    },
    [upload]
  );

  const handleFileChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      if (file) upload(file);
    },
    [upload]
  );

  // Keep-or-replace fast path: an existing source file means this step is
  // pure re-collection unless the user explicitly wants a fresh upload.
  if (hasExistingDoc && !replacing) {
    return (
      <div className='flex flex-col gap-6'>
        <div className='text-center'>
          <Heading variant='cardTitle' as='h2'>
            You already have a source file
          </Heading>
          <Text variant='caption' className='mt-1 text-text-secondary'>
            Your master experience document is already built from a previous
            upload. Keep it, or upload a new file to rebuild it.
          </Text>
        </div>
        <div className='flex items-center justify-center gap-3'>
          <Button
            name='onboarding-replace-source-file'
            variant='outline'
            size='sm'
            onClick={() => setReplacing(true)}
          >
            Upload a new file
          </Button>
          <Button
            name='onboarding-keep-source-file'
            variant='primary'
            size='sm'
            onClick={onComplete}
          >
            Keep it and continue
          </Button>
        </div>
        <div className='text-center'>
          <Button
            name='onboarding-skip-upload'
            variant='bare'
            className='text-text-secondary hover:bg-surface-elevated hover:text-text-primary'
            size='sm'
            onClick={onSkip}
          >
            Skip this step
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className='flex flex-col gap-6'>
      <div className='text-center'>
        <Heading variant='cardTitle' as='h2'>
          Upload your resume
        </Heading>
        <Text variant='caption' className='mt-1 text-text-secondary'>
          We&apos;ll parse it to build your master experience document.
        </Text>
      </div>

      <Card
        padding='lg'
        className={cn(
          'cursor-pointer border-dashed transition-colors',
          dragOver && 'border-brand-500 bg-brand-500/5',
          uploaded && 'border-success bg-success/5'
        )}
        onClick={() => !uploading && !uploaded && inputRef.current?.click()}
        onDragOver={e => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={handleDrop}
        role='button'
        tabIndex={0}
        onKeyDown={e => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            if (!uploading && !uploaded) inputRef.current?.click();
          }
        }}
        aria-label='Upload resume file'
      >
        <div
          aria-live='polite'
          className='flex flex-col items-center gap-3 py-4'
        >
          {uploading ? (
            <>
              <Spinner size='lg' aria-label='Uploading resume' />
              <Text variant='body' className='text-text-secondary'>
                {parseStage}
              </Text>
            </>
          ) : uploaded ? (
            <>
              <CheckCircle className='size-12 text-success' aria-hidden />
              <Text variant='body' className='text-success'>
                Resume uploaded successfully!
              </Text>
            </>
          ) : (
            <>
              <div className='rounded-full bg-surface-tertiary p-4'>
                {dragOver ? (
                  <FileText className='size-8 text-brand-500' aria-hidden />
                ) : (
                  <Upload className='size-8 text-text-tertiary' aria-hidden />
                )}
              </div>
              <div className='text-center'>
                <Text variant='body'>
                  Drop your resume here or click to browse
                </Text>
                <Text variant='caption' className='mt-1 text-text-tertiary'>
                  PDF or DOCX, up to 10 MB
                </Text>
              </div>
            </>
          )}
        </div>
      </Card>

      <input
        ref={inputRef}
        type='file'
        accept='.pdf,.docx'
        onChange={handleFileChange}
        className='hidden'
        aria-hidden='true'
        data-sentry-mask
      />

      {error && <Alert variant='error'>{error}</Alert>}

      <div className='text-center'>
        <Button
          name='onboarding-skip-upload'
          variant='bare'
          className='text-text-secondary hover:bg-surface-elevated hover:text-text-primary'
          size='sm'
          onClick={onSkip}
          disabled={uploading}
        >
          Skip this step
        </Button>
      </div>
    </div>
  );
}
