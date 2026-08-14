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
  createBareTarget,
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
}

function readSuggestionsCache(): MatchedSuggestion[] | null {
  try {
    const raw = sessionStorage.getItem(SUGGESTIONS_CACHE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as CachedSuggestions;
    if (!Array.isArray(parsed.matches) || parsed.matches.length === 0) {
      return null;
    }
    if (Date.now() - parsed.cachedAt > SUGGESTIONS_CACHE_TTL_MS) return null;
    return parsed.matches;
  } catch {
    return null; // corrupt / unavailable storage — treat as no cache
  }
}

function writeSuggestionsCache(matches: MatchedSuggestion[]): void {
  try {
    sessionStorage.setItem(
      SUGGESTIONS_CACHE_KEY,
      JSON.stringify({ cachedAt: Date.now(), matches })
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
  const [createdLabel, setCreatedLabel] = useState<string | null>(null);
  const [draftingResume, setDraftingResume] = useState(false);
  const [suggestions, setSuggestions] = useState<MatchedSuggestion[]>([]);
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
        setSuggestions(cached);
        setSelected(new Set(cached.map(m => m.suggestion.label)));
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
          // Pre-select all suggestions
          setSelected(new Set(data.matches.map(m => m.suggestion.label)));
          writeSuggestionsCache(data.matches);
        }
      } catch (err) {
        if (!cancelled) {
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

  const toggleSelection = useCallback((label: string) => {
    setSelected(prev => {
      const next = new Set(prev);
      if (next.has(label)) {
        next.delete(label);
      } else {
        next.add(label);
      }
      return next;
    });
  }, []);

  const handleRefreshSuggestions = useCallback(() => {
    // Deliberate reroll: drop the cached set and re-run the (billed)
    // suggest pass. The nonce bump re-fires the fetch effect above.
    clearSuggestionsCache();
    setError(null);
    setSuggestions([]);
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
    let created = 0;

    for (const match of suggestions) {
      if (!selected.has(match.suggestion.label)) continue;
      try {
        let targetId: string;

        if (match.is_new) {
          // Bare create, NOT from-manual: these suggestions already went
          // through LLM matching in ``/targets/suggest`` — re-running the
          // create-or-link endpoint would repeat that call per target.
          const createdTarget = await createBareTarget({
            label: match.suggestion.label,
            description: match.suggestion.description,
          });
          targetId = createdTarget.id;
        } else {
          targetId = match.matched_target!.id;
        }

        await linkTarget(targetId);
        created++;

        // Fire the activation pipeline without awaiting — onboarded
        // targets were previously left at ``activation_status=idle``
        // because the wizard only ran create+link, so no jobs got polled
        // until the user manually clicked Activate on /targets.
        activateTargetInBackground(targetId);
      } catch {
        // Continue creating remaining targets
      }
    }

    setCreatedCount(created);
    onTargetsCreated?.(created);
    setCreating(false);
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

  // Post-creation success
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
