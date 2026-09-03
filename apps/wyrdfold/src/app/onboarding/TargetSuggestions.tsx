'use client';

import { useEffect, useState, useCallback, useRef } from 'react';
import { useRouter } from 'next/navigation';
import { CheckCircle, Target } from 'lucide-react';
import { Badge } from '@danieljoffe/shared-ui/Badge';
import { Card } from '@danieljoffe/shared-ui/Card';
import { Text } from '@danieljoffe/shared-ui/Text';
import { Heading } from '@danieljoffe/shared-ui/Heading';
import { Spinner } from '@danieljoffe/shared-ui/Spinner';
import { Alert } from '@danieljoffe/shared-ui/Alert';
import Button from '@/components/kit/Button';
import { extractApiError } from '@/lib/extractApiError';
import { cn } from '@/lib/cn';
import {
  activateTargetInBackground,
  createOrLinkTarget,
  linkTarget,
} from '@/app/(app)/targets/targetFlows';
import type {
  MatchedSuggestion,
  MatchedSuggestions,
} from '@/app/(app)/targets/types';
import { completeOnboarding } from './completeOnboarding';
import type { JobData } from './JobUrlInput';
import { useStagedMessage } from './useStagedMessage';

/**
 * Path A's promised payoff: after the from-posting target lands, draft
 * the tailored resume for that very posting and finish onboarding ON the
 * review page instead of a dashboard detour ("a tailored resume right
 * away", PathChooser). Returns true only when the draft exists AND the
 * onboarding-complete flag is confirmed persisted (navigating without
 * the flag recreates the redirect-loop bug completeOnboarding documents).
 * Any failure — no JD, gap gate, LLM budget, network — returns false and
 * the caller falls back to the normal completion flow: the target is
 * already created, so the user lost nothing but the shortcut.
 *
 * The JD is threaded through from the add-job step's manual-add response
 * rather than re-fetched: ``GET /api/jobs/{id}`` gates on a ``scores``
 * row existing for one of the caller's targets, and the from-posting
 * target only gets scores once its background activation runs — so the
 * fetch 404'd on every fresh onboarding and the payoff never fired.
 */
async function draftPathAResume(
  postingId: string,
  descriptionHtml: string | null
): Promise<boolean> {
  const jd = (descriptionHtml ?? '').trim();
  if (!jd) return false;
  try {
    const res = await fetch('/api/jobs/tailor/resume', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        job_description: jd,
        job_posting_id: postingId,
      }),
    });
    if (!res.ok) return false;

    return await completeOnboarding();
  } catch {
    return false;
  }
}

const ANALYZE_STAGES = [
  'Analyzing your experience...',
  'Matching roles to your background — a few more seconds...',
] as const;

// Per-tab cache of the last suggestion set (sweep 2026-08-14 A3). The
// suggest call is a fresh LLM pass — non-deterministic and billed — so a
// mid-step refresh used to reroll the options: sets shrank or changed
// entirely, and the user paid another ~20 s wait for the privilege.
// sessionStorage survives exactly the reload case and dies with the tab;
// the TTL bounds staleness across a same-tab redo-onboarding run.
const SUGGESTIONS_CACHE_KEY = 'wyrdfold.onboarding.suggestions';
const SUGGESTIONS_CACHE_TTL_MS = 30 * 60 * 1000;

interface CachedSuggestions {
  cachedAt: number;
  matches: MatchedSuggestion[];
  /** Active-target headroom captured with the set (#864). Absent on
   *  pre-#864 cache entries — treated as "unknown", i.e. no cap. */
  remaining?: number | null;
}

function readSuggestionsCache(): {
  matches: MatchedSuggestion[];
  remaining: number | null;
} | null {
  try {
    const raw = sessionStorage.getItem(SUGGESTIONS_CACHE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as CachedSuggestions;
    if (!Array.isArray(parsed.matches) || parsed.matches.length === 0) {
      return null;
    }
    if (Date.now() - parsed.cachedAt > SUGGESTIONS_CACHE_TTL_MS) return null;
    return { matches: parsed.matches, remaining: parsed.remaining ?? null };
  } catch {
    return null; // corrupt / unavailable storage — treat as no cache
  }
}

function writeSuggestionsCache(
  matches: MatchedSuggestion[],
  remaining: number | null
): void {
  try {
    sessionStorage.setItem(
      SUGGESTIONS_CACHE_KEY,
      JSON.stringify({ cachedAt: Date.now(), matches, remaining })
    );
  } catch {
    // Quota / unavailable storage — the cache is best-effort.
  }
}

function clearSuggestionsCache(): void {
  try {
    sessionStorage.removeItem(SUGGESTIONS_CACHE_KEY);
  } catch {
    // Nothing to do — absence is the goal.
  }
}

interface TargetSuggestionsProps {
  onComplete: () => void;
  onSkip: () => void;
  /** Reports how many targets this step actually created/linked, so the
   *  completion screen can branch its copy on it — a zero-target finish
   *  must not claim "you're all set" (sweep 2026-08-14 P2). */
  onTargetsCreated?: (count: number) => void;
  jobData?: JobData | null;
}

export default function TargetSuggestions({
  onComplete,
  onSkip,
  onTargetsCreated,
  jobData,
}: TargetSuggestionsProps) {
  const router = useRouter();
  const [loading, setLoading] = useState(true);
  // The suggest call runs a full LLM pass over the master doc (~20s
  // observed); stage the copy so the wait doesn't read as a hang.
  const analyzeStage = useStagedMessage(ANALYZE_STAGES, loading);
  const [error, setError] = useState<string | null>(null);
  // Distinct from ``error``: suggestions are DERIVED from the experience
  // profile, so skipping the resume step makes this failure certain rather
  // than exceptional. Showing it in red as "No experience profile found"
  // reads as a malfunction one click after the user chose to skip — see the
  // catch block below.
  const [needsResume, setNeedsResume] = useState(false);
  const [createdLabel, setCreatedLabel] = useState<string | null>(null);
  const [draftingResume, setDraftingResume] = useState(false);
  const [suggestions, setSuggestions] = useState<MatchedSuggestion[]>([]);
  // Active-target headroom from the suggest response (#864). null = unknown
  // (older cache entry / older API) — behave as before: no client-side cap.
  const [remaining, setRemaining] = useState<number | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [creating, setCreating] = useState(false);
  const [createdCount, setCreatedCount] = useState(0);
  // Bumped by "Refresh suggestions" — a deliberate reroll that bypasses
  // (and replaces) the per-tab cache.
  const [refreshNonce, setRefreshNonce] = useState(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  // Path A: auto-create target from job posting
  useEffect(() => {
    if (!jobData) return;
    const postingId = jobData.postingId;
    const descriptionHtml = jobData.descriptionHtml;
    let cancelled = false;

    async function createFromPosting() {
      try {
        const res = await fetch(`/api/targets/from-posting/${postingId}`, {
          method: 'POST',
        });
        if (!res.ok) {
          // LLM-budgeted route — surface the structured
          // ``llm_budget_exceeded`` 429 detail (PR #701) when present,
          // but keep the friendly "you can create one manually"
          // fallback for unknown errors. Pass empty fallback to
          // ``extractApiError`` so we can distinguish "no useful
          // detail" (only the status code came back) from "real
          // server message".
          const detail = await extractApiError(res, '');
          throw new Error(detail);
        }
        const data = (await res.json()) as { id: string; label: string };
        if (!cancelled) {
          setCreatedLabel(data.label);
          // The from-posting target exists regardless of whether the
          // resume shortcut below fires — if we fall back to the
          // completion screen, it should know one target was created.
          onTargetsCreated?.(1);
          // Kick off the derive → poll → score pipeline so the new
          // target actually has matched jobs by the time the user
          // lands on /dashboard. Path B (suggest) does this after
          // ``/link``; path A (from-posting) was missing it, so the
          // user landed on an empty Top Matches block.
          activateTargetInBackground(data.id);
          // Deliver the promised payoff: draft the tailored resume for
          // this posting and finish on its review page. Fallback on any
          // failure is the pre-existing flow (completion screen).
          setDraftingResume(true);
          const drafted = await draftPathAResume(postingId, descriptionHtml);
          if (cancelled) return;
          if (drafted) {
            router.push(`/jobs/${postingId}/resume`);
            return;
          }
          setDraftingResume(false);
          timerRef.current = setTimeout(onComplete, 2000);
        }
      } catch (err) {
        if (!cancelled) {
          const message = err instanceof Error ? err.message.trim() : '';
          // ``message`` shaped like " (500)" / "(429)" means
          // ``extractApiError`` only had a status code to work with —
          // prefer the friendly fallback in that case. A real detail
          // string (e.g., "LLM hourly budget reached...") wins.
          const onlyStatus = /^\(\d+\)$/.test(message);
          setError(
            message && !onlyStatus
              ? message
              : 'Could not auto-create target. You can create one manually.'
          );
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    createFromPosting();
    return () => {
      cancelled = true;
    };
  }, [jobData, onComplete, onTargetsCreated, router]);

  /** True when the account has no optimized experience doc to suggest from. */
  async function isMissingExperienceProfile(): Promise<boolean> {
    try {
      const res = await fetch('/api/career/experience/optimized');
      if (res.status === 404) return true;
      if (!res.ok) return false;
      const body = (await res.json()) as { payload?: unknown } | null;
      return !body || !body.payload;
    } catch {
      // Can't tell — fall through to the generic error rather than claiming
      // a cause we have not established.
      return false;
    }
  }

  // Paths B/C: fetch suggestions from LLM (or serve the per-tab cache)
  useEffect(() => {
    if (jobData) return;
    let cancelled = false;

    // A plain (re)mount — most importantly a page refresh mid-step —
    // reuses the cached set instead of rerolling it. ``refreshNonce > 0``
    // is a deliberate reroll via "Refresh suggestions" and bypasses.
    if (refreshNonce === 0) {
      const cached = readSuggestionsCache();
      if (cached) {
        setSuggestions(cached.matches);
        setRemaining(cached.remaining);
        setSelected(
          new Set(
            cached.matches
              .slice(0, cached.remaining ?? cached.matches.length)
              .map(m => m.suggestion.label)
          )
        );
        setLoading(false);
        return;
      }
    }

    async function fetchSuggestions() {
      try {
        const res = await fetch('/api/targets/suggest', { method: 'POST' });
        if (!res.ok) {
          const detail = await extractApiError(res, '');
          throw new Error(detail);
        }
        const data = (await res.json()) as MatchedSuggestions;
        if (!cancelled && data.matches?.length > 0) {
          setSuggestions(data.matches);
          // Pre-select what the plan can actually hold (#864): the wizard
          // used to pre-select everything and offer "Create 3 targets" on a
          // 2-target plan — the third then bounced off the cap mid-loop.
          // The suggest response carries the headroom; unknown (null) keeps
          // the old select-everything behavior.
          const headroom = data.allowance?.remaining ?? null;
          setRemaining(headroom);
          setSelected(
            new Set(
              data.matches
                .slice(0, headroom ?? data.matches.length)
                .map(m => m.suggestion.label)
            )
          );
          writeSuggestionsCache(data.matches, headroom);
        }
      } catch (err) {
        if (!cancelled) {
          // Is this simply "you skipped the resume"? Ask the server rather
          // than pattern-matching its wording, which would break the moment
          // that copy is reworded. Only on the failure path, so the happy
          // path costs nothing.
          if (await isMissingExperienceProfile()) {
            if (!cancelled) setNeedsResume(true);
            return;
          }
          // Same friendly-fallback pattern as the from-posting branch:
          // surface a real server detail (LLM budget exceeded etc.)
          // when present, fall back to the manual-creation hint when
          // we only got a bare status code.
          const message = err instanceof Error ? err.message.trim() : '';
          const onlyStatus = /^\(\d+\)$/.test(message);
          setError(
            message && !onlyStatus
              ? message
              : 'Could not generate suggestions. You can create targets manually.'
          );
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    fetchSuggestions();
    return () => {
      cancelled = true;
    };
  }, [jobData, refreshNonce]);

  const toggleSelection = useCallback(
    (label: string) => {
      setSelected(prev => {
        const next = new Set(prev);
        if (next.has(label)) {
          next.delete(label);
        } else {
          // Selecting beyond the plan's headroom would only queue a refusal
          // (#864) — refuse the selection instead, and the hint above the
          // cards says why. Deselect one to pick another.
          if (remaining !== null && next.size >= remaining) return prev;
          next.add(label);
        }
        return next;
      });
    },
    [remaining]
  );

  const handleRefreshSuggestions = useCallback(() => {
    // Deliberate reroll: drop the cached set and re-run the (billed)
    // suggest pass. The nonce bump re-fires the fetch effect above.
    clearSuggestionsCache();
    setError(null);
    setSuggestions([]);
    setRemaining(null);
    setSelected(new Set());
    setLoading(true);
    setRefreshNonce(n => n + 1);
  }, []);

  const handleCreateSelected = useCallback(async () => {
    // Either exit consumes the offer — a later wizard run should get a
    // fresh set, not this tab's leftovers.
    clearSuggestionsCache();
    if (selected.size === 0) {
      onComplete();
      return;
    }

    setCreating(true);
    // Clear any banner from an earlier step/attempt so an error can never
    // outlive the action that caused it (#857).
    setError(null);
    let created = 0;
    const failures: string[] = [];

    for (const match of suggestions) {
      if (!selected.has(match.suggestion.label)) continue;
      try {
        let targetId: string;

        if (match.is_new) {
          // Server-side create-or-link (#864): the old bare-create → link
          // dance orphaned a catalog row whenever the link was refused
          // (create 201, link 409) — and DELETE /targets is membership-
          // scoped, so the orphan was unreachable even by its creator.
          // ``/from-suggestion`` fits this call exactly: the label is
          // already canonical (the suggest pass produced it), so no
          // re-normalization LLM call runs — the objection that ruled out
          // ``/from-manual`` here. Label only — the suggestion's description
          // is résumé-informed ("…given your work at <employer>") and
          // `targets` is the SHARED catalog, readable by every co-follower
          // and outliving the author's account (#868). The link lands
          // inactive; the background activation below flips it, same end
          // state as before.
          const result = await createOrLinkTarget(
            '/api/targets/from-suggestion',
            { label: match.suggestion.label },
            'Failed to create target'
          );
          targetId = result.target.id;
        } else {
          targetId = match.matched_target!.id;
          await linkTarget(targetId);
        }

        created++;

        // Fire the activation pipeline without awaiting — onboarded
        // targets were previously left at ``activation_status=idle``
        // because the wizard only ran create+link, so no jobs got polled
        // until the user manually clicked Activate on /targets.
        activateTargetInBackground(targetId);
      } catch (err) {
        // Keep going so one refusal doesn't strand the rest — but do NOT
        // discard the reason. Both helpers already throw the API's own
        // message via ``extractApiError``, and the two that actually happen
        // here are actionable: a 409 for the active-target cap ("you're on
        // Starter, which allows 2") and a 402 for an exhausted or expired
        // trial. Swallowing them is what let the wizard advance to "You're
        // all set!" after refusing the user's request (#857, #864).
        failures.push(err instanceof Error ? err.message.trim() : '');
      }
    }

    setCreatedCount(created);
    onTargetsCreated?.(created);
    setCreating(false);

    if (failures.length > 0) {
      const reason = failures.find(Boolean) ?? '';
      if (created === 0) {
        // Nothing was created — do NOT advance. The completion screen would
        // read as "you chose not to add a target" when in fact we refused.
        setError(
          reason || 'We couldn’t create your targets. Please try again.'
        );
        return;
      }
      // Partial success: the created targets are real, but do NOT
      // auto-advance (#864) — the success panel used to render OVER this
      // banner, so the reason never displayed and 1.5s later the wizard
      // said "You're all set!" about a request it had partly refused. The
      // panel now shows the banner and an explicit Continue.
      setError(
        `Created ${created} of ${selected.size} targets.${reason ? ` ${reason}` : ''}`
      );
      return;
    }

    timerRef.current = setTimeout(onComplete, 1500);
  }, [selected, suggestions, onComplete, onTargetsCreated]);

  // Path A: auto-creation in progress or completed
  if (jobData) {
    if (loading) {
      return (
        <div className='flex flex-col items-center gap-4 py-12'>
          <Spinner size='lg' aria-label='Creating target' />
          <Text variant='body' className='text-text-secondary'>
            Setting up a target from your job posting...
          </Text>
        </div>
      );
    }

    if (createdLabel) {
      return (
        <div className='flex flex-col items-center gap-6'>
          <Card padding='lg' className='w-full text-center'>
            <div className='flex flex-col items-center gap-3 py-4'>
              <CheckCircle className='size-12 text-success' aria-hidden />
              <div>
                <Text variant='body' className='font-medium'>
                  Target created
                </Text>
                <Text variant='caption' className='mt-1 text-text-secondary'>
                  {createdLabel}
                </Text>
              </div>
              {draftingResume && (
                <div className='mt-2 flex items-center gap-2'>
                  <Spinner size='sm' aria-label='Drafting tailored resume' />
                  <Text variant='caption' className='text-text-secondary'>
                    Drafting your tailored resume…
                  </Text>
                </div>
              )}
            </div>
          </Card>
        </div>
      );
    }

    // Error fallback — show manual flow below
  }

  // Paths B/C: suggestions or manual prompt
  if (loading) {
    return (
      <div className='flex flex-col items-center gap-4 py-12'>
        <Spinner size='lg' aria-label='Loading suggestions' />
        <Text variant='body' className='text-text-secondary'>
          {analyzeStage}
        </Text>
      </div>
    );
  }

  // Post-creation success — full or partial. On a partial refusal the
  // banner carries the API's reason and advancing is the user's explicit
  // click, never a timer (#864).
  if (createdCount > 0) {
    return (
      <div className='flex flex-col items-center gap-6'>
        <Card padding='lg' className='w-full text-center'>
          <div className='flex flex-col items-center gap-3 py-4'>
            <CheckCircle className='size-12 text-success' aria-hidden />
            <div>
              <Text variant='body' className='font-medium'>
                {createdCount === 1
                  ? '1 target created'
                  : `${createdCount} targets created`}
              </Text>
            </div>
          </div>
        </Card>
        {error && (
          <>
            <Alert variant='error' className='w-full'>
              {error}
            </Alert>
            <Button
              name='onboarding-continue-after-partial'
              variant='primary'
              size='sm'
              onClick={onComplete}
            >
              Continue
            </Button>
          </>
        )}
      </div>
    );
  }

  // Suggestions available — show selectable cards
  if (suggestions.length > 0) {
    return (
      <div className='flex flex-col gap-6'>
        <div className='text-center'>
          <Heading variant='cardTitle' as='h2'>
            Suggested targets
          </Heading>
          <Text variant='caption' className='mt-1 text-text-secondary'>
            Based on your experience, we suggest these role targets. Select the
            ones you&apos;d like to track.
          </Text>
          {remaining !== null && suggestions.length > remaining && (
            <Text variant='caption' className='mt-1 text-text-secondary'>
              Your plan has room for {remaining} active{' '}
              {remaining === 1 ? 'target' : 'targets'} right now, so you can
              select up to {remaining} here — deselect one to pick another.
            </Text>
          )}
        </div>

        <div className='flex flex-col gap-3'>
          {suggestions.map(match => {
            const { suggestion } = match;
            const isSelected = selected.has(suggestion.label);
            return (
              <Card
                key={suggestion.label}
                padding='lg'
                className={cn(
                  'cursor-pointer transition-colors',
                  isSelected
                    ? 'border-brand-500 bg-brand-500/5'
                    : 'hover:border-border-hover'
                )}
                onClick={() => toggleSelection(suggestion.label)}
                role='checkbox'
                aria-checked={isSelected}
                tabIndex={0}
                aria-label={suggestion.label}
                onKeyDown={e => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    toggleSelection(suggestion.label);
                  }
                }}
              >
                <div className='flex items-start gap-4'>
                  <div
                    className={cn(
                      'mt-0.5 flex size-5 shrink-0 items-center justify-center rounded border-2 transition-colors',
                      isSelected
                        ? 'border-brand-500 bg-brand-500'
                        : 'border-border'
                    )}
                  >
                    {isSelected && (
                      <CheckCircle
                        className='size-3.5 text-white'
                        aria-hidden
                      />
                    )}
                  </div>
                  <div className='flex-1'>
                    <div className='flex items-center gap-2'>
                      <Text variant='body' className='font-medium'>
                        {/*
                          When the LLM-suggested label fuzzy-matches an
                          existing catalog target, ``link`` actually
                          attaches the user to ``matched_target`` — which
                          may have a different label than what the LLM
                          proposed. Showing ``suggestion.label`` here
                          was misleading: e.g. the LLM suggests
                          "Full-Stack Engineer" but the linked target is
                          "Staff Full-Stack Engineer", and the user
                          finds the latter on their dashboard with no
                          explanation for the rename.
                        */}
                        {!match.is_new && match.matched_target
                          ? match.matched_target.label
                          : suggestion.label}
                      </Text>
                      {!match.is_new && (
                        <Badge variant='default' size='sm'>
                          Existing
                        </Badge>
                      )}
                    </div>
                    <Text
                      variant='caption'
                      className='mt-0.5 text-text-secondary'
                    >
                      {suggestion.description}
                    </Text>
                    {suggestion.core_skills.length > 0 && (
                      <div className='mt-2 flex flex-wrap gap-1.5'>
                        {suggestion.core_skills.map(skill => (
                          <span
                            key={skill}
                            className='rounded-full bg-surface-tertiary px-2.5 py-0.5 text-xs text-text-secondary'
                          >
                            {skill}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              </Card>
            );
          })}
        </div>

        {needsResume && (
          <Alert variant='info'>
            Suggested targets are built from your resume, and that step was
            skipped — so there is nothing to suggest from yet. Use{' '}
            <b>Change path</b> above to go back and add it, or create a target
            yourself below.
          </Alert>
        )}
        {error && <Alert variant='error'>{error}</Alert>}

        <div className='flex items-center justify-between'>
          <div className='flex items-center gap-2'>
            <Button
              name='onboarding-skip-targets'
              variant='bare'
              className='text-text-secondary hover:bg-surface-elevated hover:text-text-primary'
              size='sm'
              onClick={onSkip}
            >
              Skip this step
            </Button>
            <Button
              name='onboarding-refresh-suggestions'
              variant='bare'
              className='text-text-tertiary hover:bg-surface-elevated hover:text-text-primary'
              size='sm'
              onClick={handleRefreshSuggestions}
              disabled={creating}
            >
              Refresh suggestions
            </Button>
          </div>
          <Button
            name='onboarding-create-targets'
            variant='primary'
            size='sm'
            onClick={handleCreateSelected}
            disabled={creating}
          >
            {creating ? (
              <>
                <Spinner size='sm' aria-label='Creating targets' />
                <span>Creating...</span>
              </>
            ) : selected.size === 0 ? (
              'Continue without targets'
            ) : selected.size === 1 ? (
              'Create 1 target'
            ) : (
              `Create ${selected.size} targets`
            )}
          </Button>
        </div>
      </div>
    );
  }

  // No suggestions (error or empty OptimizedDoc) — fallback to manual prompt
  return (
    <div className='flex flex-col gap-6'>
      <div className='text-center'>
        <Heading variant='cardTitle' as='h2'>
          Set up your job targets
        </Heading>
        <Text variant='caption' className='mt-1 text-text-secondary'>
          Targets define the types of roles you&apos;re looking for. Create one
          to start tracking jobs.
        </Text>
      </div>

      {needsResume && (
        <Alert variant='info'>
          Suggested targets are built from your resume, and that step was
          skipped — so there is nothing to suggest from yet. Use{' '}
          <b>Change path</b> above to go back and add it, or create a target
          yourself below.
        </Alert>
      )}
      {error && <Alert variant='error'>{error}</Alert>}

      <Card
        padding='lg'
        className='cursor-pointer transition-colors hover:border-brand-500'
        onClick={onComplete}
        role='button'
        tabIndex={0}
        aria-label='Create your first target'
        onKeyDown={e => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            onComplete();
          }
        }}
      >
        <div className='flex items-center gap-4'>
          <div className='rounded-lg bg-surface-tertiary p-3'>
            <Target className='size-5 text-text-secondary' aria-hidden />
          </div>
          <div className='flex-1'>
            <Text variant='body' className='font-medium'>
              Create your first target
            </Text>
            <Text variant='caption' className='mt-0.5 text-text-secondary'>
              Define a role type, and we&apos;ll score and track matching jobs.
            </Text>
          </div>
        </div>
      </Card>

      <div className='text-center'>
        <Button
          name='onboarding-skip-targets'
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
