'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import Link from 'next/link';
import { formatCompanyName } from '@/lib/formatCompanyName';
import { localDateStamp } from '@/lib/localDateStamp';
import {
  ArrowLeft,
  ShieldCheck,
  Download,
  Lock,
  MoreVertical,
  RotateCcw,
  Unlock,
} from 'lucide-react';
import { Dropdown } from '@danieljoffe/shared-ui/Dropdown';
import type { DropdownItem } from '@danieljoffe/shared-ui/Dropdown';
import { Badge } from '@danieljoffe/shared-ui/Badge';
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
import { LocalDateTime, LocalNumber } from '@/components/LocalFormat';
import type {
  JobPosting,
  LintViolation,
  ResumeVersion,
  ResumeVersionsResponse,
  AtsRecheckResponse,
  TailoredDocumentState,
  TailoredResumeRecord,
  TailorResponse,
} from '../../types';

interface CoverLetterReviewPageProps {
  jobPostingId: string;
}

const AUTOSAVE_DEBOUNCE_MS = 1500;

/** Re-generation is backgrounded (#656): the POST returns 202 and the pipeline
 *  runs ~27s server-side. Same cadence as ``useTailorDocument``. */
const REGEN_POLL_INTERVAL_MS = 2500;
const REGEN_MAX_POLLS = 96; // ~4 minutes

type SaveStatus = 'idle' | 'pending' | 'saving' | 'saved' | 'error';

const delay = (ms: number) =>
  new Promise<void>(resolve => setTimeout(resolve, ms));

/**
 * Poll the by-job route until a letter NEWER than `previousId` lands.
 *
 * Anchored on the record id, never on "a record exists": the route returns the
 * most recent document, and while a re-generation is in flight it reports the
 * PREVIOUS letter with `status: 'idle'` — the API's `_document_state` reads the
 * record first and lets it win. The id is therefore the only usable completion
 * signal on this surface.
 *
 * Returns null when the run failed, the poll broke, or the ceiling was hit. The
 * server keeps going past the ceiling, so that is a "reload in a moment", not a
 * cancellation.
 */
async function waitForNewLetter(
  jobPostingId: string,
  previousId: string
): Promise<TailoredResumeRecord | null> {
  for (let attempt = 0; attempt < REGEN_MAX_POLLS; attempt += 1) {
    await delay(REGEN_POLL_INTERVAL_MS);
    let state: TailoredDocumentState;
    try {
      const res = await fetch(
        `/api/jobs/tailor/by-job/${jobPostingId}/cover-letter`
      );
      if (!res.ok) return null;
      state = (await res.json()) as TailoredDocumentState;
    } catch {
      return null;
    }
    if (state.record && state.record.id !== previousId) return state.record;
    if (state.status === 'error') return null;
  }
  return null;
}

function slugify(value: string): string {
  return (
    value
      .toLowerCase()
      .normalize('NFKD')
      .replace(/\p{Diacritic}/gu, '')
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '') || 'cover-letter'
  );
}

export default function CoverLetterReviewPage({
  jobPostingId,
}: CoverLetterReviewPageProps) {
  const { toast } = useToast();

  const [posting, setPosting] = useState<JobPosting | null>(null);
  const [record, setRecord] = useState<TailoredResumeRecord | null>(null);
  const [markdown, setMarkdown] = useState('');
  const [saveStatus, setSaveStatus] = useState<SaveStatus>('idle');
  // Filename the user types in the download field; empty string means
  // "fall back to the slug-derived default". Reset on reload.
  const [customFilename, setCustomFilename] = useState('');

  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  // Posting 404'd (pre-scoring window) — degrade the chrome, never gate the
  // document. See the load() comment and #724 for the diagnosed race.
  const [postingMissing, setPostingMissing] = useState(false);
  const [approving, setApproving] = useState(false);
  const [unapproving, setUnapproving] = useState(false);
  const [regenerating, setRegenerating] = useState(false);
  const [lintWarnings, setLintWarnings] = useState<LintViolation[]>([]);
  const [confirmRegenerateOpen, setConfirmRegenerateOpen] = useState(false);
  // The version awaiting restore confirmation; null when no dialog is open.
  const [versionToRestore, setVersionToRestore] =
    useState<ResumeVersion | null>(null);

  const [versions, setVersions] = useState<ResumeVersion[] | null>(null);
  const [versionCap, setVersionCap] = useState<number>(5);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [versionsOpen, setVersionsOpen] = useState(false);

  const defaultFilename = useMemo(() => {
    if (!record) return '';
    const name =
      (record.payload as { contact?: { name?: string } }).contact?.name ??
      'cover-letter';
    // Without the posting (still scoring — see ``postingMissing``) there is
    // no company to slug; a name-date filename beats blocking the download.
    return posting
      ? `${slugify(name)}-${slugify(formatCompanyName(posting.company_name))}-cover-letter-${localDateStamp()}`
      : `${slugify(name)}-cover-letter-${localDateStamp()}`;
  }, [record, posting]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [jobRes, letterRes] = await Promise.all([
        fetch(`/api/jobs/${jobPostingId}`),
        fetch(`/api/jobs/tailor/by-job/${jobPostingId}/cover-letter`),
      ]);
      // ``GET /jobs/{id}`` gates on a ``scores`` row, so a just-added manual
      // posting 404s until background scoring lands (#724's race, fixed here
      // for the sibling page). A missing posting degrades the chrome —
      // subtitle, filename slug, regenerate — but never gates the letter:
      // the document is the caller's own, fetched per-user.
      if (letterRes.status === 404) {
        setNotFound(true);
        return;
      }
      if (!letterRes.ok || (!jobRes.ok && jobRes.status !== 404)) {
        toast({ variant: 'error', title: 'Failed to load cover letter' });
        return;
      }
      if (jobRes.status === 404) {
        setPostingMissing(true);
      } else {
        setPosting((await jobRes.json()) as JobPosting);
      }
      // #656 envelope: this route returns {record, status, message}, not a
      // bare record. Reading it as a record silently yielded an undefined id
      // and empty markdown — the page rendered but was inert.
      const state = (await letterRes.json()) as TailoredDocumentState;
      if (!state.record) {
        setNotFound(true);
        return;
      }
      setRecord(state.record);
      setMarkdown(state.record.payload_md ?? '');
      setSaveStatus('idle');
    } catch {
      toast({ variant: 'error', title: 'Network error loading cover letter' });
    } finally {
      setLoading(false);
    }
  }, [jobPostingId, toast]);

  useEffect(() => {
    load();
  }, [load]);

  const [rechecking, setRechecking] = useState(false);

  async function handleRecheck() {
    if (!record) return;
    setRechecking(true);
    try {
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
      toast({
        variant: 'error',
        title: 'Network error re-checking cover letter',
      });
    } finally {
      setRechecking(false);
    }
  }

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
    // Never PATCH a locked record — same approve-vs-debounce race as the
    // resume page (a keystroke during the flush→approve flight re-arms the
    // timer, which then 409s against the lock; observed live 2026-08-06).
    if (record.approved_at !== null) {
      setSaveStatus('saved');
      return true;
    }
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
        toast({ variant: 'error', title: 'Cover letter failed ATS lint' });
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
      setMarkdown(curr =>
        curr === sentMarkdown ? (data.record.payload_md ?? curr) : curr
      );
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

  useEffect(() => {
    if (saveStatus !== 'pending') return;
    const timer = setTimeout(() => {
      persistMarkdown();
    }, AUTOSAVE_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [markdown, saveStatus, persistMarkdown]);

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
      // Best-effort.
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
      await recordCheckpoint();
      const res = await fetch(`/api/jobs/tailor/${record.id}/approve`, {
        method: 'POST',
      });
      if (!res.ok) {
        toast({
          variant: 'error',
          title: await extractApiError(res, 'Failed to lock cover letter'),
        });
        return;
      }
      const approved = (await res.json()) as TailoredResumeRecord;
      setRecord(approved);
      // Disarm any auto-save re-armed mid-approve (see persistMarkdown's
      // approved_at guard — this stops the debounce timer from firing).
      setSaveStatus('saved');
      toast({ variant: 'success', title: 'Cover letter locked' });
    } catch {
      toast({
        variant: 'error',
        title: 'Network error locking cover letter',
      });
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
          title: await extractApiError(res, 'Failed to unlock cover letter'),
        });
        return;
      }
      const reopened = (await res.json()) as TailoredResumeRecord;
      setRecord(reopened);
      toast({ variant: 'success', title: 'Cover letter unlocked for editing' });
    } catch {
      toast({
        variant: 'error',
        title: 'Network error unlocking cover letter',
      });
    } finally {
      setUnapproving(false);
    }
  }

  async function handleDownload() {
    if (!record) return;
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
      // download attribute with ``/`` confuses the OS file picker.
      const safe = (customFilename.trim() || defaultFilename).replace(
        /[\\/]/g,
        '_'
      );
      a.download = `${safe}.docx`;
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      toast({
        variant: 'error',
        title: 'Network error downloading cover letter',
      });
    }
  }

  async function handleRegenerate() {
    if (!record || !posting) return;
    const previousId = record.id;
    setRegenerating(true);
    try {
      await flushPendingSave();
      await recordCheckpoint();
      const res = await fetch('/api/jobs/tailor/cover-letter', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          // REQUIRED by ``CoverLetterRequest`` (min_length=1, no default).
          // Omitting it 422'd every re-generate launched from this page —
          // the button has never worked. The record's own snapshot is the
          // right JD: it is the text this letter was written against, and
          // the posting fetch on this route doesn't carry a description.
          job_description: record.jd_snapshot,
          job_posting_id: jobPostingId,
          // Display-cleaned: this string is the letter's addressee — feed
          // junk ("003 Humana Inc.") and the LLM writes to it verbatim.
          company_name: formatCompanyName(posting.company_name),
          role_title: posting.title,
          // #785: carry this letter's own stretch opt-in forward. Without it
          // a re-generate on a Skip-verdict job can come back a refusal and
          // replace a letter the user already chose to have written. The
          // verdict can't be re-derived here — it is per-(job, target) and
          // this route has no target in scope — so the record is the record.
          ...(record.allow_stretch ? { allow_stretch: true } : {}),
        }),
      });
      if (!res.ok) {
        // LLM-budgeted route — without ``extractApiError`` here, hitting
        // the daily/hourly cap would render as the generic
        // "Re-generation failed" instead of the structured "$X of $Y
        // budget reached" message (PR #701).
        toast({
          variant: 'error',
          title: await extractApiError(res, 'Re-generation failed'),
        });
        return;
      }
      // The kick-off returns 202 and the pipeline runs detached for ~27s, so
      // the modal closes here rather than on completion — the user is free to
      // leave, the run persists either way. Re-reading immediately (what this
      // used to do) always returned the OLD letter under a success toast.
      setConfirmRegenerateOpen(false);
      const next = await waitForNewLetter(jobPostingId, previousId);
      if (!next) {
        toast({
          variant: 'error',
          title: 'Still re-generating — reload in a moment to see the letter.',
        });
        return;
      }
      setRecord(next);
      setMarkdown(next.payload_md ?? '');
      setSaveStatus('idle');
      setLintWarnings([]);
      setVersions(null);
      toast({ variant: 'success', title: 'Cover letter re-generated with AI' });
    } catch {
      toast({
        variant: 'error',
        title: 'Network error re-generating cover letter',
      });
    } finally {
      setRegenerating(false);
    }
  }

  function restoreVersion(version: ResumeVersion) {
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
    // Mirrors ResumeReviewPage — snapshot the live draft before
    // ``setMarkdown(md)`` so the autosave that follows doesn't
    // overwrite the live document without leaving a recoverable
    // entry in version history.
    await flushPendingSave();
    await recordCheckpoint();
    setMarkdown(md);
    setSaveStatus('pending');
    setVersionsOpen(false);
    setVersionToRestore(null);
  }

  // ``(app)/layout.tsx`` already supplies the page's ``<main>``
  // landmark — wrapping page content in a second ``<main>`` here
  // gives SR users two main landmarks per page (WCAG 1.3.1).
  if (notFound) {
    return (
      <div className='mx-auto max-w-4xl p-6'>
        <Heading variant='hero' as='h1'>
          Cover letter not found
        </Heading>
        <Text variant='body'>
          We couldn&rsquo;t find a cover letter for this job. Generate one from
          the job page first.
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

  if (loading || !record || (!posting && !postingMissing)) {
    return (
      <div
        className='mx-auto max-w-4xl space-y-4 p-6'
        aria-label='Loading cover letter'
        role='status'
      >
        {/* Back link */}
        <Skeleton className='h-5 w-24' />

        {/* Hero h1 "Review cover letter" + body subtitle ("Job Title — Company"). */}
        <div className='space-y-2'>
          <Skeleton variant='rectangular' className='h-10 w-80' />
          <Skeleton className='h-4 w-72' />
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
            {
              label: crumbLabel(posting?.title ?? 'Job'),
              href: `/jobs/${jobPostingId}`,
            },
            { label: 'Cover letter' },
          ]}
        />
        {isApproved && (
          <Badge variant='success' size='sm'>
            Locked
          </Badge>
        )}
      </div>

      <div>
        <Heading variant='hero' as='h1'>
          Review cover letter
        </Heading>
        <Text variant='body' className='text-text-secondary'>
          {posting ? (
            <>
              {posting.title} &mdash; {formatCompanyName(posting.company_name)}
            </>
          ) : (
            // Scoring hasn't linked the posting to a target yet, so the
            // job detail is temporarily unavailable — the letter isn't.
            'Job details are still processing — they’ll appear here shortly.'
          )}
        </Text>
      </div>

      {/* Flagged draft (#656): this letter was generated and KEPT despite
          failing ATS lint — same treatment as a resume, since it runs the
          same linter and costs the same daily-cap slot. */}
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

      {/* Cost only — tokens/model/latency are developer telemetry (ux-sweep
          2026-08-12 §C12); they stay reachable in the native tooltip. */}
      <div className='flex flex-wrap gap-x-4 gap-y-1 rounded-md bg-surface-secondary px-3 py-2'>
        <Text
          variant='meta'
          as='span'
          title={`${record.input_tokens + record.output_tokens} tokens · ${
            record.model ?? 'unknown model'
          } · ${(record.latency_ms / 1000).toFixed(1)}s`}
        >
          Generated for ${record.cost_usd.toFixed(4)}
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
                        <LocalDateTime value={v.created_at} />
                      </Text>
                    </span>
                    {!isApproved && (
                      <Button
                        name={`restore-version-${v.id}`}
                        variant='bare'
                        className='text-text-secondary hover:bg-surface-elevated hover:text-text-primary'
                        size='sm'
                        // Disambiguate "Load" for SR users when
                        // multiple versions exist — see the same
                        // pattern in ResumeReviewPage.
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
            <label htmlFor='cover-letter-filename' className='sr-only'>
              Download filename
            </label>
            <input
              id='cover-letter-filename'
              type='text'
              value={customFilename}
              placeholder={defaultFilename}
              onChange={e => setCustomFilename(e.target.value)}
              maxLength={120}
              disabled={isApproved}
              className='min-w-0 flex-1 rounded border border-border bg-surface px-2 py-1 text-sm text-text-primary placeholder:text-text-tertiary focus:outline-none focus:ring-2 focus:ring-brand-500 disabled:opacity-60'
              aria-describedby='cover-letter-filename-suffix'
            />
            <Text
              variant='meta'
              as='span'
              className='text-text-tertiary'
              id='cover-letter-filename-suffix'
            >
              .docx
            </Text>
          </div>
          {/* Same rationale as ResumeReviewPage: Download stays as
              a standalone icon (frequent, free, non-destructive);
              Re-generate (LLM-billed) and Lock/Unlock move behind a
              ``⋮`` menu to prevent mis-tap of high-cost actions. */}
          <div className='flex items-center gap-1'>
            <Button
              name='download-cover-letter-docx'
              variant='bare'
              className='text-text-secondary hover:bg-surface-elevated hover:text-text-primary'
              size='sm'
              iconOnly
              aria-label='Download cover letter as .docx'
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
                  label: 'Re-generate with AI',
                  icon: <RotateCcw className='size-4' aria-hidden />,
                  onClick: () => setConfirmRegenerateOpen(true),
                  // ``!posting``: regeneration POSTs the posting's company +
                  // title, which aren't available in the pre-scoring window —
                  // disabled beats a silently no-op confirm.
                  disabled:
                    regenerating ||
                    approving ||
                    saveStatus === 'saving' ||
                    isApproved ||
                    !posting,
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
          ariaLabel='Cover letter markdown'
          value={markdown}
          onChange={next => {
            setMarkdown(next);
            setSaveStatus('pending');
          }}
          disabled={isApproved || regenerating || approving || unapproving}
        />
        <div className='flex items-center justify-between gap-2'>
          <Text
            variant='meta'
            as='span'
            className='text-text-tertiary'
            aria-live='polite'
          >
            {regenerating
              ? 'Re-generating with AI — this takes about 30 seconds'
              : !isApproved && saveLabel(saveStatus)}
          </Text>
          <Text variant='meta' as='span' className='text-text-tertiary'>
            <LocalNumber value={markdown.length} /> chars
          </Text>
        </div>
      </div>

      <ConfirmModal
        isOpen={confirmRegenerateOpen}
        onClose={() => setConfirmRegenerateOpen(false)}
        onConfirm={handleRegenerate}
        title='Re-generate cover letter?'
        message={
          isApproved
            ? 'Generate a new cover letter from scratch? This will replace the approved letter — the current one stays in version history but will no longer be the active draft.'
            : 'Re-generate this cover letter from scratch? Current draft is saved as a version first.'
        }
        confirmLabel='Regenerate'
        loading={regenerating}
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
