'use client';

import { useState } from 'react';
import { formatCompanyName } from '@/lib/formatCompanyName';
import Link from 'next/link';
import { ArrowRight, ArrowUpRight, Check } from 'lucide-react';
import { Badge } from '@danieljoffe/shared-ui/Badge';
import { Heading } from '@danieljoffe/shared-ui/Heading';
import { Text } from '@danieljoffe/shared-ui/Text';
import Button from '@/components/kit/Button';
import LinkButton from '@/components/kit/LinkButton';
import { formatLocation } from '@/lib/formatLocation';
import { timeAgo } from '@/lib/timeAgo';
import { useToast } from '@/state/Toast/ToastProvider';
// `/search` is now a top-level (auth-adaptive) route; the targets flow stays in
// the authed `(app)` group, so it's imported by absolute alias.
import { createOrLinkTarget } from '@/app/(app)/targets/targetFlows';
// Reused from the explorer (the original "export CompanyAvatar /
// AddToTargetMenu from JobSearchExplorer" decision — moving them out is a
// candidate follow-up now that three surfaces import them).
import { AddToTargetMenu, CompanyAvatar } from './JobSearchExplorer';
import { emitSearchEvent } from './searchEvents';
import type { JobSearchResult, TargetRef } from './types';
import type { TargetsSource } from './useTargetsSource';

export interface ListingDetailBodyProps {
  job: JobSearchResult;
  /** The caller's targets that already contain this listing (empty = unbound). */
  inTargets: TargetRef[];
  /** Logged-out: the detail shows info + source link + ONE soft signup allusion
   *  (#467 §11.5) instead of the bind / LLM actions. Authed: unchanged. */
  isAuthenticated: boolean;
  targetsSource: TargetsSource;
  /** Fired when the user binds this listing to a target, so the host can
   *  reflect it live — flipping the detail to its bound state (match/tailor
   *  unlock) (#467 §11.3). `| undefined` for exactOptionalPropertyTypes: the
   *  modal frame passes its own optional prop straight through. */
  onAddedToTarget?: ((target: TargetRef) => void) | undefined;
}

/** Brand-coloured "✓ In '{label}'" status line — the pipeline state, echoing the
 *  card badge. `text-text-brand` is the theme-adaptive brand token (light + dark). */
function InTargetLine({ label }: { label: string }) {
  return (
    <span className='inline-flex items-center gap-1.5 text-sm font-semibold text-text-brand'>
      <Check className='size-4 shrink-0' aria-hidden />
      In “{label}”
    </span>
  );
}

/**
 * The listing detail CONTENT (#467 §11.2/§11.3) — header, chips, snippet, and
 * the contextual actions. Extracted from {@link JobDetailModal} when the detail
 * became URL-addressable (§11.2 fast-follow): the intercepted `/search/[id]`
 * modal renders this inside the shared-ui Modal (via JobDetailModal, unchanged),
 * while the hard-load `/search/[id]` page renders the SAME content in page
 * layout — one detail, two frames, nothing to drift.
 *
 * Browsing stays free (info + source link); the LLM actions are **target-level**
 * and so gate on target-membership:
 *   - UNBOUND → the two bind actions (add-to-target / create-target).
 *   - BOUND   → the LLM actions unlock (match / tailor), scoped to the target.
 */
export default function ListingDetailBody({
  job,
  inTargets,
  isAuthenticated,
  targetsSource,
  onAddedToTarget,
}: ListingDetailBodyProps) {
  // Map the picker's `{ id, label }` to the membership `{ target_id, label }`.
  const handleAdded = (target: { id: string; label: string }) =>
    onAddedToTarget?.({ target_id: target.id, label: target.label });
  const { toast } = useToast();
  const [creating, setCreating] = useState(false);
  const bound = inTargets.length > 0;
  const locationDisplay = formatLocation(job);
  const meta = [formatCompanyName(job.company_name), locationDisplay]
    .filter(Boolean)
    .join(' · ');
  const chips: string[] = [
    job.salary_text || 'Salary not listed',
    `Posted ${timeAgo(job.source_posted_at ?? job.cataloged_at)}`,
    ...(locationDisplay ? [locationDisplay] : []),
  ];

  // Create a target from this listing (#467 power-action) — the from-url derive
  // (§11.3 bind action). Reuses the shared create-or-link flow: derives a
  // scoring profile in the background, DEDUPS against your existing targets,
  // links inactive, and registers the ATS board. The from-url endpoint 422s
  // without an experience profile, surfaced as an error toast.
  const createTarget = async () => {
    if (!job.absolute_url) return;
    setCreating(true);
    try {
      const result = await createOrLinkTarget(
        '/api/targets/from-url',
        { jd_url: job.absolute_url },
        'Couldn’t create target'
      );
      toast({
        variant: 'success',
        title: result.was_matched
          ? `Linked to “${result.target.label}”`
          : `Target created: “${result.target.label}”`,
        description:
          'Building your match profile in the background — activate it under Targets to see your matches.',
      });
    } catch (e) {
      toast({
        variant: 'error',
        title: 'Couldn’t create target',
        ...(e instanceof Error ? { description: e.message } : {}),
      });
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className='flex flex-col gap-4'>
      {/* Header — monogram · role · company·location · source link. `pr-8`
          clears the Modal's absolute ✕ (top-4 right-4) when framed as a modal;
          on the full page it's harmless breathing room. */}
      <div className='flex items-start gap-3 pr-8'>
        <CompanyAvatar name={formatCompanyName(job.company_name)} size='lg' />
        <div className='min-w-0 flex-1'>
          <Heading variant='component' as='h2' className='text-balance'>
            {job.title}
          </Heading>
          {meta && (
            <Text variant='meta' as='p' className='mt-0.5 text-text-secondary'>
              {meta}
            </Text>
          )}
          {job.absolute_url && (
            <a
              href={job.absolute_url}
              target='_blank'
              rel='noopener noreferrer'
              className='mt-1.5 inline-flex items-center gap-1 text-sm font-semibold text-text-brand underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2'
            >
              View original posting
              <ArrowUpRight className='size-3.5 shrink-0' aria-hidden />
            </a>
          )}
        </div>
      </div>

      {/* Chips — salary / posted / location. */}
      <div className='flex flex-wrap gap-2'>
        {chips.map(c => (
          <Badge key={c} variant='default' size='md'>
            {c}
          </Badge>
        ))}
      </div>

      {/* Snippet — the same preview shown on the card. */}
      {job.snippet && (
        <Text variant='body' as='p' className='text-text-secondary'>
          {job.snippet}
        </Text>
      )}

      {/* Actions. Logged-out → ONE soft, non-blocking signup allusion (§11.5):
          browsing stays free; we allude to the member value (match + tailor)
          and link to signup — never the bind / LLM actions. Logged-in → the
          bind→unlock crux (§11.3): unbound shows the two bind actions, bound
          unlocks match/tailor. */}
      {!isAuthenticated ? (
        <div className='flex flex-col gap-2 rounded-lg border border-brand-500/25 bg-brand-500/5 p-4'>
          <Text
            variant='body'
            as='p'
            className='font-semibold text-text-primary'
          >
            There’s more here when you’re signed in
          </Text>
          <Text variant='meta' as='p' className='text-text-secondary'>
            Members see how this role matches their profile and can auto-tailor
            a résumé to it — free with an account.
          </Text>
          <Link
            href='/login'
            onClick={() => {
              // The public funnel's conversion tick (§10 PR6):
              // fire-and-forget with keepalive, so it survives the
              // navigation and can never delay it.
              emitSearchEvent({
                event_type: 'signup_click',
                surface: 'public',
                job_posting_id: job.id,
              });
            }}
            className='mt-1 inline-flex items-center gap-1 self-start text-sm font-semibold text-text-brand underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2'
          >
            Sign up free
            <ArrowRight className='size-3.5 shrink-0' aria-hidden />
          </Link>
        </div>
      ) : bound ? (
        <div className='flex flex-col gap-3 border-t border-border pt-4'>
          <InTargetLine label={inTargets[0].label} />
          {/* Match + tailor are LINKS to the existing matched surfaces
              (/jobs/[id], /jobs/[id]/resume) — a deliberate reuse rather than
              an inline re-implementation (flagged for review in the PR). */}
          <LinkButton
            name='search-detail-see-match'
            href={`/jobs/${job.id}`}
            variant='primary'
            size='sm'
            className='self-start'
          >
            See how you match
          </LinkButton>
          <div className='flex flex-wrap items-center gap-2'>
            <LinkButton
              name='search-detail-tailor'
              href={`/jobs/${job.id}/resume`}
              variant='outline'
              size='sm'
            >
              Tailor a résumé
            </LinkButton>
            <AddToTargetMenu
              jobId={job.id}
              source={targetsSource}
              label='Add to another target'
              onAdded={handleAdded}
            />
          </div>
        </div>
      ) : (
        <div className='flex flex-col gap-3 border-t border-border pt-4'>
          <Text variant='meta' as='p' className='text-text-tertiary'>
            Add to a target to unlock LLM pipelines.
          </Text>
          <div className='flex flex-wrap items-center gap-2'>
            <AddToTargetMenu
              jobId={job.id}
              source={targetsSource}
              variant='primary'
              onAdded={handleAdded}
            />
            {job.absolute_url && (
              <Button
                name='search-detail-create-target'
                variant='secondary'
                size='sm'
                onClick={createTarget}
                disabled={creating}
                aria-busy={creating}
              >
                {creating ? 'Creating…' : 'Create a target from this role'}
              </Button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
