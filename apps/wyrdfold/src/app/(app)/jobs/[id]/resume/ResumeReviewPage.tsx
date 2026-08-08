'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import Link from 'next/link';
import {
  ArrowLeft,
  Download,
  Lock,
  MoreVertical,
  RotateCcw,
  ShieldCheck,
  Unlock,
} from 'lucide-react';
import { Badge } from '@danieljoffe/shared-ui/Badge';
import { Dropdown } from '@danieljoffe/shared-ui/Dropdown';
import type { DropdownItem } from '@danieljoffe/shared-ui/Dropdown';
import { Heading } from '@danieljoffe/shared-ui/Heading';
import { Skeleton } from '@danieljoffe/shared-ui/Skeleton';
import { Spinner } from '@danieljoffe/shared-ui/Spinner';
import { Text } from '@danieljoffe/shared-ui/Text';
import Button from '@/components/kit/Button';
import ConfirmModal from '@/components/ConfirmModal';
import MarkdownPreviewEditor from '@/components/MarkdownPreviewEditor';
import { extractApiError } from '@/lib/extractApiError';
import { useToast } from '@/state/Toast/ToastProvider';
import Breadcrumbs, { crumbLabel } from '@/components/kit/Breadcrumbs';
import { isFlaggedDraft } from '../../types';
import type {
  AtsRecheckResponse,
  JobPosting,
  LintViolation,
  ResumeVersion,
  ResumeVersionsResponse,
  TailoredResumeRecord,
  TailorResponse,
} from '../../types';
import { useTailorDocument } from '../../useTailorDocument';

interface ResumeReviewPageProps {
  jobPostingId: string;
}

const AUTOSAVE_DEBOUNCE_MS = 1500;

type SaveStatus = 'idle' | 'pending' | 'saving' | 'saved' | 'error';

function slugify(value: string): string {
  return (
    value
      .toLowerCase()
      .normalize('NFKD')
      .replace(/\p{Diacritic}/gu, '')
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '') || 'resume'
  );
}

export default function ResumeReviewPage({
  jobPostingId,
}: ResumeReviewPageProps) {
  const { toast } = useToast();

  const [posting, setPosting] = useState<JobPosting | null>(null);
  const [markdown, setMarkdown] = useState('');
  const [saveStatus, setSaveStatus] = useState<SaveStatus>('idle');
  // Filename the user types in the download field; empty string means
  // "fall back to the slug-derived default". Reset on reload (we don't
  // persist a custom name on the resume row in v1).
  const [customFilename, setCustomFilename] = useState('');

  // The document itself, its 202+poll state, and re-adapt all live in the
  // shared hook (#656). Landing here while a generation kicked off from the
  // job panel is still running renders the in-progress state and picks the
  // poll up — the run outlives the navigation that started it.
  const {
    record,
    loading: recordLoading,
    generating,
    error: generationError,
    generate,
    setRecord,
  } = useTailorDocument({ jobPostingId, kind: 'resume' });

  const [postingLoading, setPostingLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [approving, setApproving] = useState(false);
  const [unapproving, setUnapproving] = useState(false);
  const [rechecking, setRechecking] = useState(false);
  const [lintWarnings, setLintWarnings] = useState<LintViolation[]>([]);
  const [confirmReadaptOpen, setConfirmReadaptOpen] = useState(false);
  // The version awaiting restore confirmation; null when no dialog is open.
  const [versionToRestore, setVersionToRestore] =
    useState<ResumeVersion | null>(null);

  const [versions, setVersions] = useState<ResumeVersion[] | null>(null);
  const [versionCap, setVersionCap] = useState<number>(5);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [versionsOpen, setVersionsOpen] = useState(false);

  // Slug-derived filename baseline. Recomputed when the loaded record
  // or posting changes. Falls back to a literal when ``contact.name``
  // is absent — the production API populates it, but unit-test fixtures
  // often elide payload internals.
  const defaultFilename = useMemo(() => {
    if (!record || !posting) return '';
    const name =
      (record.payload as { contact?: { name?: string } }).contact?.name ??
      'resume';
    return `${slugify(name)}-${slugify(posting.company_name)}-${new Date().toISOString().slice(0, 10)}`;
  }, [record, posting]);

  // The posting is fetched independently of the document — the hook owns the
  // document's lifecycle, and the two have different "missing" semantics.
  const loadPosting = useCallback(async () => {
    setPostingLoading(true);
    try {
      const jobRes = await fetch(`/api/jobs/${jobPostingId}`);
      if (jobRes.status === 404) {
        setNotFound(true);
        return;
      }
      if (!jobRes.ok) {
        toast({ variant: 'error', title: 'Failed to load resume' });
        return;
      }
      setPosting((await jobRes.json()) as JobPosting);
    } catch {
      toast({ variant: 'error', title: 'Network error loading resume' });
    } finally {
      setPostingLoading(false);
    }
  }, [jobPostingId, toast]);

  useEffect(() => {
    loadPosting();
  }, [loadPosting]);

  // Adopt a document's markdown when a DIFFERENT document arrives — first
  // load, or a re-adapt that inserted a new row. Deliberately keyed on the id
  // rather than the record identity: a PATCH/approve round-trip returns the
  // same document, and `persistMarkdown` already merges that result against
  // whatever the user has typed since. Re-syncing there would clobber
  // in-flight edits.
  const loadedRecordIdRef = useRef<string | null>(null);
  useEffect(() => {
    if (!record || loadedRecordIdRef.current === record.id) return;
    loadedRecordIdRef.current = record.id;
    setMarkdown(record.payload_md ?? '');
    setSaveStatus('idle');
  }, [record]);

  // A background generation that failed has no record to show — surface why
  // rather than leaving the page on a bare "not found".
  useEffect(() => {
    if (generationError) toast({ variant: 'error', title: generationError });
  }, [generationError, toast]);

  const loadVersions = useCallback(async () => {
    if (!record) return;
    setVersionsLoading(true);
    try {
      const res = await fetch(`/api/jobs/tailor/${record.id}/versions`);
      if (!res.ok) {
        toast({
          variant: 'error',
          title: await extractApiError(res, 'Failed to load version history'),
        });
        return;
      }
      const data = (await res.json()) as ResumeVersionsResponse;
      setVersions(data.versions);
      setVersionCap(data.cap);
    } catch {
      toast({ variant: 'error', title: 'Network error loading versions' });
    } finally {
      setVersionsLoading(false);
    }
  }, [record, toast]);

  function toggleVersions() {
    const next = !versionsOpen;
    setVersionsOpen(next);
    if (next && versions === null) loadVersions();
  }

  const inflightRef = useRef(false);
  const persistMarkdown = useCallback(async (): Promise<boolean> => {
    if (!record) return false;
    // Never PATCH a locked record: an edit or version-restore landing
    // around the approve flight re-arms the debounce, and its timer used
    // to outlive the lock — PATCH → 409 "already approved" + an error
    // toast right after the green "Resume locked" one (observed live
    // 2026-08-06, twice). Approval also sets saveStatus 'saved' to disarm
    // the timer; this guard is the invariant if any other path re-arms it.
    if (record.approved_at !== null) {
      setSaveStatus('saved');
      return true;
    }
    // Single-flight: a slow PATCH overlapping the next debounce tick would
    // race to overwrite the row. Skip; the next keystroke or explicit
    // flushPendingSave will retry.
    if (inflightRef.current) return false;
    inflightRef.current = true;
    const sentMarkdown = markdown;
    setSaveStatus('saving');
    setLintWarnings([]);
    try {
      const res = await fetch(`/api/jobs/tailor/${record.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ markdown: sentMarkdown }),
      });
      if (res.status === 422) {
        const err = await res.json();
        toast({ variant: 'error', title: 'Resume failed ATS lint' });
        if (err.detail?.violations) {
          setLintWarnings(err.detail.violations as LintViolation[]);
        }
        setSaveStatus('error');
        return false;
      }
      if (!res.ok) {
        toast({
          variant: 'error',
          title: await extractApiError(res, 'Failed to save changes'),
        });
        setSaveStatus('error');
        return false;
      }
      const data = (await res.json()) as TailorResponse;
      setRecord(data.record);
      setLintWarnings(data.lint_warnings);
      // Only adopt server-normalized markdown if the user hasn't typed
      // since we sent — otherwise their in-flight edits would be lost.
      setMarkdown(curr =>
        curr === sentMarkdown ? (data.record.payload_md ?? curr) : curr
      );
      // Likewise, only flip to 'saved' if no new edit pushed us back to
      // 'pending' during the in-flight fetch.
      setSaveStatus(prev => (prev === 'saving' ? 'saved' : prev));
      return true;
    } catch {
      toast({ variant: 'error', title: 'Network error saving draft' });
      setSaveStatus('error');
      return false;
    } finally {
      inflightRef.current = false;
    }
  }, [record, markdown, toast]);

  // Debounced auto-save: every keystroke moves saveStatus to 'pending';
  // 1.5s of quiet then flushes a PATCH.
  useEffect(() => {
    if (saveStatus !== 'pending') return;
    const timer = setTimeout(() => {
      persistMarkdown();
    }, AUTOSAVE_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [markdown, saveStatus, persistMarkdown]);

  // Session-end checkpoint: snapshot the current markdown into version
  // history when the user navigates away. Uses sendBeacon so the browser
  // delivers the request even after the tab is gone. Server-side dedup
  // keeps the 5-version cap from being eaten by no-op closes.
  const sessionStateRef = useRef({
    saveStatus,
    markdown,
    recordId: record?.id ?? null,
  });
  useEffect(() => {
    sessionStateRef.current = {
      saveStatus,
      markdown,
      recordId: record?.id ?? null,
    };
  });
  useEffect(() => {
    const flush = () => {
      const {
        saveStatus: status,
        markdown: md,
        recordId,
      } = sessionStateRef.current;
      if (!recordId || status === 'idle') return;
      const url = `/api/jobs/tailor/${recordId}/checkpoint`;
      const carryUnsaved = status === 'pending' || status === 'error';
      const payload = carryUnsaved ? JSON.stringify({ markdown: md }) : '{}';
      navigator.sendBeacon(
        url,
        new Blob([payload], { type: 'application/json' })
      );
    };
    window.addEventListener('pagehide', flush);
    return () => window.removeEventListener('pagehide', flush);
  }, []);

  const flushPendingSave = useCallback(async (): Promise<boolean> => {
    if (saveStatus === 'pending' || saveStatus === 'saving') {
      return persistMarkdown();
    }
    return saveStatus !== 'error';
  }, [saveStatus, persistMarkdown]);

  const recordCheckpoint = useCallback(async (): Promise<void> => {
    if (!record) return;
    try {
      await fetch(`/api/jobs/tailor/${record.id}/checkpoint`, {
        method: 'POST',
      });
    } catch {
      // Checkpoint is best-effort — don't block approve/readapt on it.
    }
  }, [record]);

  async function handleApprove() {
    if (!record) return;
    setApproving(true);
    try {
      const ok = await flushPendingSave();
      if (!ok) {
        setApproving(false);
        return;
      }
      // Snapshot the about-to-be-locked draft into version history.
      await recordCheckpoint();
      const res = await fetch(`/api/jobs/tailor/${record.id}/approve`, {
        method: 'POST',
      });
      if (!res.ok) {
        toast({
          variant: 'error',
          title: await extractApiError(res, 'Failed to approve resume'),
        });
        return;
      }
      const approved = (await res.json()) as TailoredResumeRecord;
      setRecord(approved);
      // Disarm any auto-save re-armed by an edit/restore that landed during
      // the flush→approve flight — its debounce timer would PATCH the
      // now-locked record into a 409. (persistMarkdown also guards on
      // approved_at; this stops the timer from even firing.)
      setSaveStatus('saved');
      toast({ variant: 'success', title: 'Resume locked' });
    } catch {
      toast({ variant: 'error', title: 'Network error locking resume' });
    } finally {
      setApproving(false);
    }
  }

  async function handleUnapprove() {
    if (!record) return;
    setUnapproving(true);
    try {
      const res = await fetch(`/api/jobs/tailor/${record.id}/unapprove`, {
        method: 'POST',
      });
      if (!res.ok) {
        toast({
          variant: 'error',
          title: await extractApiError(res, 'Failed to unlock resume'),
        });
        return;
      }
      const reopened = (await res.json()) as TailoredResumeRecord;
      setRecord(reopened);
      toast({ variant: 'success', title: 'Resume unlocked for editing' });
    } catch {
      toast({ variant: 'error', title: 'Network error unlocking resume' });
    } finally {
      setUnapproving(false);
    }
  }

  async function handleDownload() {
    if (!record || !posting) return;
    const ok = await flushPendingSave();
    if (!ok) return;
    try {
      const res = await fetch(`/api/jobs/tailor/${record.id}/download`);
      if (!res.ok) {
        toast({
          variant: 'error',
          title: await extractApiError(res, 'Download failed'),
        });
        return;
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      // Strip path separators on save — browsers tolerate them but a
      // download attribute with ``/`` confuses the OS file picker. We
      // don't lowercase / slugify the user's input here: if they typed
      // capital letters or spaces, that's their intent.
      const safe = (customFilename.trim() || defaultFilename).replace(
        /[\\/]/g,
        '_'
      );
      a.download = `${safe}.docx`;
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      toast({ variant: 'error', title: 'Network error downloading resume' });
    }
  }

  async function handleReadapt() {
    if (!record || !record.job_posting_id) return;
    // Snapshot the current draft before regenerating so users can
    // restore it from version history if the new generation is worse.
    await flushPendingSave();
    await recordCheckpoint();
    // The modal closes on kick-off, not on completion: the POST returns 202
    // and the run outlives this page, so holding a modal open for ~39s would
    // pin the user to a screen they're free to leave. The toolbar's
    // "Regenerating…" state carries the progress from here.
    setConfirmReadaptOpen(false);
    setVersions(null);
    // `generate` handles the 202, polls for the NEW document id (the old one
    // is still the most recent until the run lands), and surfaces budget /
    // gap-gate / concurrency errors through `generationError`.
    const ok = await generate({
      job_description: record.jd_snapshot,
      job_posting_id: record.job_posting_id,
      force_fresh: true,
    });
    if (ok) {
      toast({ variant: 'success', title: 'Resume re-adapted with AI' });
    }
  }

  async function handleRecheck() {
    if (!record) return;
    setRechecking(true);
    try {
      // Flush any pending edit first — re-checking stale markdown would
      // report violations the user has already fixed on screen.
      const flushed = await flushPendingSave();
      if (!flushed) return;
      const res = await fetch(`/api/jobs/tailor/${record.id}/ats-recheck`, {
        method: 'POST',
      });
      if (!res.ok) {
        toast({
          variant: 'error',
          title: await extractApiError(res, 'ATS re-check failed'),
        });
        return;
      }
      const data = (await res.json()) as AtsRecheckResponse;
      setRecord(data.record);
      setLintWarnings(data.violations.filter(v => v.severity === 'warning'));
      toast({
        variant: data.ok ? 'success' : 'error',
        title: data.ok
          ? 'Passes ATS checks'
          : `${data.violations.filter(v => v.severity === 'error').length} ATS issue(s) remain`,
      });
    } catch {
      toast({ variant: 'error', title: 'Network error re-checking resume' });
    } finally {
      setRechecking(false);
    }
  }

  function restoreVersion(version: ResumeVersion) {
    // Versions before the markdown pivot stored only structured payload.
    // Newer versions include payload_md. We fall back to current markdown
    // if the snapshot has no markdown to restore.
    const md = (version as ResumeVersion & { payload_md?: string | null })
      .payload_md;
    if (!md) {
      toast({
        variant: 'error',
        title: 'This version predates markdown — cannot restore',
      });
      return;
    }
    setVersionToRestore(version);
  }

  async function performRestore(version: ResumeVersion) {
    const md = (version as ResumeVersion & { payload_md?: string | null })
      .payload_md;
    if (!md) return;
    // Snapshot the current draft before swapping markdown so the
    // pre-restore content is recoverable from the same version
    // history dropdown. Mirrors handleReadapt's pre-mutate pattern.
    // Without this, the autosave that fires after ``setMarkdown(md)``
    // overwrites the live document with the loaded version and the
    // unsnapshotted pre-restore content is lost.
    await flushPendingSave();
    await recordCheckpoint();
    setMarkdown(md);
    setSaveStatus('pending');
    setVersionsOpen(false);
    setVersionToRestore(null);
  }

  // The ``(app)/layout.tsx`` wrapper already supplies the page's
  // ``<main id="main-content">`` landmark. Wrapping page-level content
  // in a second ``<main>`` here gave SR users two main landmarks per
  // page (WCAG 1.3.1 / ARIA spec). Use ``<div>`` instead.
  // The poll route now answers "no document" with a 200 + null record rather
  // than a 404 (#656), so a missing draft is a settled empty state — not a
  // failed fetch. Only claim it once the fetch has settled and nothing is in
  // flight, or a page opened mid-generation would flash "not found".
  const documentMissing =
    !recordLoading && !generating && !record && !generationError;

  if (notFound || documentMissing) {
    return (
      <div className='mx-auto max-w-4xl p-6'>
        <Heading variant='hero' as='h1'>
          Tailored resume not found
        </Heading>
        <Text variant='body'>
          We couldn&rsquo;t find a tailored resume for this job. Generate one
          from the job page first.
        </Text>
        <Link
          href={`/jobs/${jobPostingId}`}
          className='mt-4 inline-flex items-center gap-1 text-brand-500 hover:text-brand-600'
        >
          <ArrowLeft className='h-4 w-4' /> Back to job
        </Link>
      </div>
    );
  }

  // Landed here while a generation is still in flight (kicked off from the
  // job panel, then navigated straight in — or a re-adapt on a page that had
  // no prior draft). The run persists server-side, so this is a wait, not a
  // dead end (#656).
  if (generating && !record) {
    return (
      <div
        className='mx-auto max-w-4xl space-y-4 p-6'
        aria-live='polite'
        role='status'
      >
        <Breadcrumbs
          items={[
            { label: 'Jobs', href: '/jobs' },
            {
              label: crumbLabel(posting?.title ?? 'Job'),
              href: `/jobs/${jobPostingId}`,
            },
            { label: 'Resume' },
          ]}
        />
        <Heading variant='hero' as='h1'>
          Tailoring your resume
        </Heading>
        <div className='flex items-center gap-2'>
          <Spinner size='sm' aria-label='Generating resume' />
          <Text variant='body' className='text-text-secondary'>
            Usually 30&ndash;60 seconds. This keeps running if you navigate away
            &mdash; come back any time.
          </Text>
        </div>
        <Skeleton variant='rectangular' className='h-[60vh] w-full' />
      </div>
    );
  }

  if (postingLoading || recordLoading || !record || !posting) {
    return (
      <div
        className='mx-auto max-w-4xl space-y-4 p-6'
        aria-label='Loading resume'
        role='status'
      >
        {/* Back link */}
        <Skeleton className='h-5 w-24' />

        {/* Hero h1 "Review Resume" + body subtitle ("Job Title — Company"). */}
        <div className='space-y-2'>
          <Skeleton variant='rectangular' className='h-10 w-72' />
          <Skeleton className='h-4 w-80' />
        </div>

        {/* Cost stats bar */}
        <Skeleton variant='rectangular' className='h-10 w-full rounded-md' />

        {/* Version history collapsed */}
        <Skeleton variant='rectangular' className='h-10 w-full rounded-md' />

        {/* Action toolbar */}
        <div className='flex items-center justify-between'>
          <Skeleton className='h-4 w-32' />
          <div className='flex items-center gap-1'>
            {Array.from({ length: 3 }).map((_, i) => (
              <Skeleton
                key={i}
                variant='rectangular'
                className='h-8 w-8 rounded-md'
              />
            ))}
          </div>
        </div>

        {/* Markdown editor */}
        <Skeleton variant='rectangular' className='h-[60vh] w-full' />
      </div>
    );
  }

  const isApproved = record.approved_at !== null;
  const isReused =
    record.warnings?.includes('reused_from_similar_job') ?? false;
  const flagged = isFlaggedDraft(record);
  const lintErrors = (record.lint_violations ?? []).filter(
    v => v.severity === 'error'
  );

  return (
    <div className='mx-auto max-w-4xl space-y-4 p-6'>
      <div className='flex items-center justify-between'>
        <Breadcrumbs
          items={[
            { label: 'Jobs', href: '/jobs' },
            { label: crumbLabel(posting.title), href: `/jobs/${jobPostingId}` },
            { label: 'Resume' },
          ]}
        />
        <div className='flex items-center gap-2'>
          {flagged && (
            <Badge variant='error' size='sm'>
              Needs fixes
            </Badge>
          )}
          {isApproved && (
            <Badge variant='success' size='sm'>
              Locked
            </Badge>
          )}
        </div>
      </div>

      <div>
        <Heading variant='hero' as='h1'>
          Review tailored resume
        </Heading>
        <Text variant='body' className='text-text-secondary'>
          {posting.title} &mdash; {posting.company_name}
        </Text>
      </div>

      {isReused && !isApproved && (
        <div className='flex items-start gap-2 rounded-md border border-info/30 bg-info/10 p-3'>
          <Badge variant='info' size='sm'>
            Reused
          </Badge>
          <Text variant='meta' className='text-text-secondary'>
            Cloned from a similar job &mdash; no LLM cost. Edit freely or
            re-adapt with AI to regenerate from scratch.
          </Text>
        </div>
      )}

      {/* Flagged draft (#656): this resume was generated and KEPT despite
          failing ATS lint, rather than 422'd away — the LLM call is already
          paid for and regenerating burns the daily cap. Editing the markdown
          below and re-checking is free. */}
      {flagged && (
        <div className='space-y-2 rounded-md border border-error/30 bg-error/10 p-3'>
          <div className='flex items-center justify-between gap-2'>
            <Text variant='caption' className='text-error'>
              Failed ATS checks
            </Text>
            <Button
              name='ats-recheck'
              variant='secondary'
              size='sm'
              onClick={handleRecheck}
              disabled={rechecking || saveStatus === 'saving'}
            >
              {rechecking ? (
                <>
                  <Spinner size='sm' aria-label='Re-running ATS checks' />
                  <span>Checking…</span>
                </>
              ) : (
                <>
                  <ShieldCheck className='size-4' aria-hidden='true' />
                  <span>Re-run ATS checks</span>
                </>
              )}
            </Button>
          </div>
          <Text variant='meta' className='text-text-secondary'>
            This draft was saved so you don&rsquo;t lose the generation. Fix the
            issues below, then re-run the checks &mdash; it&rsquo;s free and
            instant, no AI credits.
          </Text>
          <ul className='list-inside list-disc space-y-1'>
            {lintErrors.map((v, i) => (
              <li key={i}>
                <Text variant='meta' as='span'>
                  [{v.code}] {v.message}
                </Text>
              </li>
            ))}
          </ul>
        </div>
      )}

      {lintWarnings.length > 0 && (
        <div className='rounded-md border border-warning/30 bg-warning/10 p-3'>
          <Text variant='caption' className='mb-1 text-warning'>
            ATS Lint
          </Text>
          <ul className='list-inside list-disc space-y-1'>
            {lintWarnings.map((w, i) => (
              <li key={i}>
                <Text variant='meta' as='span'>
                  [{w.code}] {w.message}
                </Text>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className='flex flex-wrap gap-x-4 gap-y-1 rounded-md bg-surface-secondary px-3 py-2'>
        <Text variant='meta' as='span'>
          Cost: ${record.cost_usd.toFixed(4)}
        </Text>
        <Text variant='meta' as='span'>
          Tokens:{' '}
          {(record.input_tokens + record.output_tokens).toLocaleString()}
        </Text>
        {record.model && (
          <Text variant='meta' as='span'>
            Model: {record.model}
          </Text>
        )}
        <Text variant='meta' as='span'>
          Latency: {(record.latency_ms / 1000).toFixed(1)}s
        </Text>
      </div>

      <div className='rounded-md border border-border'>
        <button
          type='button'
          onClick={toggleVersions}
          className='flex w-full items-center justify-between px-3 py-2 text-left hover:bg-surface-secondary'
          aria-expanded={versionsOpen}
          aria-controls='version-history-panel'
        >
          <Text variant='caption' as='span'>
            Version history{versions ? ` (${versions.length})` : ''}
          </Text>
          <Text variant='meta' as='span' className='text-text-tertiary'>
            {versionsOpen ? 'Hide' : 'Show'}
          </Text>
        </button>
        {versionsOpen && (
          <div
            id='version-history-panel'
            className='space-y-2 border-t border-border px-3 py-2'
          >
            <Text variant='meta' className='text-text-tertiary'>
              Free tier keeps the last {versionCap} versions. Older edits are
              dropped automatically.
            </Text>
            {versionsLoading && <Skeleton className='h-6 w-full' />}
            {!versionsLoading && versions !== null && versions.length === 0 && (
              <Text variant='meta' className='text-text-tertiary'>
                No prior versions yet.
              </Text>
            )}
            {!versionsLoading && versions !== null && versions.length > 0 && (
              <ul className='space-y-1'>
                {versions.map(v => (
                  <li
                    key={v.id}
                    className='flex items-center justify-between gap-2 text-sm'
                  >
                    <span className='flex items-center gap-2'>
                      <Badge
                        variant={
                          v.source === 'initial'
                            ? 'default'
                            : v.source === 'llm_adapt'
                              ? 'info'
                              : 'success'
                        }
                        size='sm'
                      >
                        {v.source.replace('_', ' ')}
                      </Badge>
                      <Text variant='meta' as='span'>
                        {new Date(v.created_at).toLocaleString()}
                      </Text>
                    </span>
                    {!isApproved && (
                      <Button
                        name={`restore-version-${v.id}`}
                        variant='bare'
                        className='text-text-secondary hover:bg-surface-elevated hover:text-text-primary'
                        size='sm'
                        // "Load" by itself is ambiguous to SR users
                        // when several versions are listed — they'd
                        // hear "Load" repeated with no way to
                        // distinguish. Disambiguate via the version
                        // source + timestamp.
                        aria-label={`Load ${v.source.replace('_', ' ')} version from ${new Date(v.created_at).toLocaleString()}`}
                        onClick={() => restoreVersion(v)}
                      >
                        Load
                      </Button>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>

      <div>
        <div className='mb-1 flex items-center justify-between gap-2'>
          <div className='flex min-w-0 flex-1 items-center gap-1'>
            <label htmlFor='resume-filename' className='sr-only'>
              Download filename
            </label>
            <input
              id='resume-filename'
              type='text'
              value={customFilename}
              placeholder={defaultFilename}
              onChange={e => setCustomFilename(e.target.value)}
              maxLength={120}
              disabled={isApproved}
              className='min-w-0 flex-1 rounded border border-border bg-surface px-2 py-1 text-sm text-text-primary placeholder:text-text-tertiary focus:outline-none focus:ring-2 focus:ring-brand-500 disabled:opacity-60'
              aria-describedby='resume-filename-suffix'
            />
            <Text
              variant='meta'
              as='span'
              className='text-text-tertiary'
              id='resume-filename-suffix'
            >
              .docx
            </Text>
          </div>
          {/* Download stays as a standalone icon — it's frequent,
              non-destructive, and free. Re-adapt (LLM-billed) and
              Lock/Unlock (irreversible from the lock side) move
              behind a ``⋮`` menu so they can't be mis-tapped when
              the user meant to download. Found via a real
              chrome-devtools session: the previous icon row was
              28×28 buttons in a tight cluster — adjacent 44×44
              touch targets overlapped, so a sloppy tap on
              Download could fire Re-adapt instead. */}
          <div className='flex items-center gap-1'>
            <Button
              name='download-docx'
              variant='bare'
              className='text-text-secondary hover:bg-surface-elevated hover:text-text-primary'
              size='sm'
              iconOnly
              aria-label='Download resume as .docx'
              title='Download .docx'
              onClick={handleDownload}
              disabled={saveStatus === 'saving'}
            >
              <Download className='h-4 w-4' aria-hidden='true' />
            </Button>
            <Dropdown
              align='right'
              trigger={
                <span
                  className='inline-flex h-8 w-8 items-center justify-center rounded text-text-secondary hover:bg-surface-tertiary hover:text-text-primary'
                  aria-label='More actions'
                  title='More actions'
                >
                  <MoreVertical className='h-4 w-4' aria-hidden='true' />
                  <span className='sr-only'>More actions</span>
                </span>
              }
              items={[
                {
                  // Free + deterministic, so it sits in the menu for every
                  // draft — a flagged one also gets the prominent button in
                  // the banner above, where the failure is being explained.
                  label: 'Re-run ATS checks',
                  icon: <ShieldCheck className='size-4' aria-hidden />,
                  onClick: handleRecheck,
                  disabled: rechecking || saveStatus === 'saving',
                },
                {
                  label: 'Re-adapt with AI',
                  icon: <RotateCcw className='size-4' aria-hidden />,
                  onClick: () => setConfirmReadaptOpen(true),
                  disabled:
                    generating ||
                    approving ||
                    saveStatus === 'saving' ||
                    isApproved,
                },
                ...(isApproved
                  ? [
                      {
                        label: 'Unlock for editing',
                        icon: <Unlock className='size-4' aria-hidden />,
                        onClick: handleUnapprove,
                        disabled: unapproving,
                      } satisfies DropdownItem,
                    ]
                  : [
                      {
                        label: 'Lock from editing',
                        icon: <Lock className='size-4' aria-hidden />,
                        onClick: handleApprove,
                        // Lock is mostly-irreversible (Unlock is
                        // available via the menu, but the doc
                        // status flips downstream) — mark danger
                        // so the menu styles it accordingly.
                        danger: true,
                        disabled:
                          approving ||
                          saveStatus === 'pending' ||
                          saveStatus === 'saving' ||
                          saveStatus === 'error',
                      } satisfies DropdownItem,
                    ]),
              ]}
            />
          </div>
        </div>
        <MarkdownPreviewEditor
          ariaLabel='Resume markdown'
          value={markdown}
          onChange={next => {
            setMarkdown(next);
            setSaveStatus('pending');
          }}
          disabled={isApproved || generating || approving || unapproving}
        />
        <div className='flex items-center justify-between gap-2'>
          <Text
            variant='meta'
            as='span'
            className='text-text-tertiary'
            aria-live='polite'
          >
            {generating
              ? 'Regenerating — safe to navigate away'
              : !isApproved && saveLabel(saveStatus)}
          </Text>
          <Text variant='meta' as='span' className='text-text-tertiary'>
            {markdown.length.toLocaleString()} chars
          </Text>
        </div>
      </div>

      <ConfirmModal
        isOpen={confirmReadaptOpen}
        onClose={() => setConfirmReadaptOpen(false)}
        onConfirm={handleReadapt}
        title='Re-adapt resume?'
        message={
          isApproved
            ? 'Generate a new resume from scratch? This will replace the approved resume — the current one stays in version history but will no longer be the active draft.'
            : 'Re-generate this resume from scratch? Current draft is saved as a version first.'
        }
        confirmLabel='Regenerate'
        loading={generating}
        loadingLabel='Regenerating…'
      />

      <ConfirmModal
        isOpen={versionToRestore !== null}
        onClose={() => setVersionToRestore(null)}
        onConfirm={() => {
          if (versionToRestore) void performRestore(versionToRestore);
        }}
        title='Load this version?'
        message='Load this version? Your current draft is saved as a version first so you can roll back.'
        confirmLabel='Restore'
      />
    </div>
  );
}

function saveLabel(status: SaveStatus): string {
  switch (status) {
    case 'idle':
      return '';
    case 'pending':
      return 'Editing — autosave pending';
    case 'saving':
      return 'Saving...';
    case 'saved':
      return 'Saved';
    case 'error':
      return 'Save failed — keep typing to retry';
  }
}
