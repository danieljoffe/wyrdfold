'use client';

import { useCallback, useEffect, useState } from 'react';
import { Input } from '@danieljoffe/shared-ui/Input';
import { Spinner } from '@danieljoffe/shared-ui/Spinner';
import { Text } from '@danieljoffe/shared-ui/Text';
import Button from '@/components/kit/Button';
import type { MatchedSuggestion, TargetSearchResult } from './types';

const MIN_QUERY_LENGTH = 2;
const DEBOUNCE_MS = 300;

interface TargetSearchTabProps {
  /**
   * Follow (link) a discovered target. Resolves `true` when the link
   * succeeded so the row can flip to "Following", or `false` when it failed
   * (e.g. the active-target limit) — the parent owns the toast either way.
   */
  onFollow: (target: TargetSearchResult) => Promise<boolean>;
  /**
   * Create-or-follow one AI suggestion (a brand-new label creates via the
   * LLM create-or-link endpoint; a catalog match just links). Resolves `true`
   * on success so the card can be removed. The parent owns the toast + list
   * refresh — same contract as {@link onFollow}.
   */
  onCreateSuggestion: (match: MatchedSuggestion) => Promise<boolean>;
  /**
   * Hand the typed query to the Manual tab. The zero-result state used to offer
   * only the LLM fallback, so a user who knew exactly what they wanted had to
   * switch tabs and retype it into an empty Title field.
   */
  onCreateManually: (label: string) => void;
}

/**
 * "Search existing targets" — discovery over the shared catalog. Targets are
 * shared, so a role another user already created can be followed instead of
 * recreated. Debounced substring search hits `GET /api/targets/search`.
 *
 * When the catalog has nothing for the query, the search dead-ends — so we fall
 * back to the LLM: `POST /api/targets/suggest-from-query` canonicalises the
 * query into a target plus a few adjacent roles the user can create or follow.
 * That fallback is offered in the empty state and, more quietly, beneath a
 * thin set of catalog hits ("none of these?").
 */
export default function TargetSearchTab({
  onFollow,
  onCreateSuggestion,
  onCreateManually,
}: TargetSearchTabProps) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<TargetSearchResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [searched, setSearched] = useState(false);
  const [followingId, setFollowingId] = useState<string | null>(null);

  // LLM fallback state. `aiSuggestions === null` means "not requested for this
  // query"; an array (possibly empty) means the LLM answered.
  const [aiSuggestions, setAiSuggestions] = useState<
    MatchedSuggestion[] | null
  >(null);
  const [aiLoading, setAiLoading] = useState(false);
  const [aiError, setAiError] = useState(false);
  const [creatingLabel, setCreatingLabel] = useState<string | null>(null);

  const trimmed = query.trim();

  // The AI suggestions belong to whatever query produced them — drop them the
  // moment the query changes so stale roles never linger under a new search.
  useEffect(() => {
    setAiSuggestions(null);
    setAiError(false);
    setAiLoading(false);
  }, [trimmed]);

  useEffect(() => {
    if (trimmed.length < MIN_QUERY_LENGTH) {
      setResults([]);
      setSearched(false);
      setLoading(false);
      return;
    }

    let cancelled = false;
    setLoading(true);
    const timer = setTimeout(async () => {
      try {
        const res = await fetch(
          `/api/targets/search?q=${encodeURIComponent(trimmed)}`
        );
        if (!res.ok) throw new Error('search failed');
        const payload = (await res.json()) as { results: TargetSearchResult[] };
        if (!cancelled) setResults(payload.results);
      } catch {
        if (!cancelled) setResults([]);
      } finally {
        if (!cancelled) {
          setLoading(false);
          setSearched(true);
        }
      }
    }, DEBOUNCE_MS);

    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [trimmed]);

  const handleFollow = useCallback(
    async (target: TargetSearchResult) => {
      setFollowingId(target.id);
      try {
        const ok = await onFollow(target);
        if (ok) {
          // Reflect the follow immediately — the button flips to "Following".
          setResults(prev =>
            prev.map(r => (r.id === target.id ? { ...r, is_linked: true } : r))
          );
        }
      } finally {
        setFollowingId(null);
      }
    },
    [onFollow]
  );

  const handleSuggestWithAI = useCallback(async () => {
    if (trimmed.length < MIN_QUERY_LENGTH) return;
    setAiLoading(true);
    setAiError(false);
    setAiSuggestions(null);
    try {
      const res = await fetch('/api/targets/suggest-from-query', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: trimmed }),
      });
      if (!res.ok) throw new Error('suggest failed');
      const data = (await res.json()) as { matches: MatchedSuggestion[] };
      setAiSuggestions(data.matches);
    } catch {
      setAiError(true);
    } finally {
      setAiLoading(false);
    }
  }, [trimmed]);

  const handleCreateSuggestion = useCallback(
    async (match: MatchedSuggestion) => {
      const label = match.suggestion.label;
      setCreatingLabel(label);
      try {
        const ok = await onCreateSuggestion(match);
        if (ok) {
          setAiSuggestions(prev =>
            prev ? prev.filter(m => m.suggestion.label !== label) : prev
          );
        }
      } finally {
        setCreatingLabel(null);
      }
    },
    [onCreateSuggestion]
  );

  // The AI-fallback block: the "Suggest with AI" trigger, its loading/error
  // states, and the suggestion cards. Rendered in the empty state and (as a
  // quieter link) beneath thin catalog results.
  const renderAiFallback = (variant: 'primary' | 'quiet') => {
    if (aiLoading) {
      return (
        <div className='flex items-center gap-2 py-2 text-text-secondary'>
          <Spinner size='sm' />
          <Text variant='meta'>Asking AI for role ideas…</Text>
        </div>
      );
    }

    // Not-yet-requested (or errored → null) shows the trigger. When it errored,
    // pair it with the message so the button doubles as a retry.
    if (aiSuggestions === null) {
      return (
        <div className='flex flex-col gap-2'>
          {aiError && (
            <Text variant='meta' className='text-status-error-fg'>
              Couldn’t generate suggestions. Please try again.
            </Text>
          )}
          <Button
            name='target-search-suggest-ai'
            variant={variant === 'primary' ? 'outline' : 'bare'}
            size='sm'
            onClick={handleSuggestWithAI}
            className='self-start'
          >
            {variant === 'primary'
              ? '✨ Suggest roles with AI'
              : '✨ None of these? Suggest with AI'}
          </Button>
        </div>
      );
    }

    if (aiSuggestions.length === 0) {
      return (
        <Text variant='meta' className='text-text-secondary'>
          AI didn’t find a good role for “{trimmed}”. Try the Manual or From URL
          tab.
        </Text>
      );
    }

    return (
      <div className='flex flex-col gap-2'>
        <Text variant='meta' className='text-text-tertiary'>
          AI suggestions — pick one to create or follow:
        </Text>
        <ul className='flex flex-col gap-2' aria-label='AI target suggestions'>
          {aiSuggestions.map((match, i) => {
            const { label, description, core_skills } = match.suggestion;
            const busy = creatingLabel === label;
            return (
              <li
                key={`${label}-${i}`}
                className='flex items-center justify-between gap-3 rounded-lg border border-border p-3'
              >
                <div className='flex min-w-0 flex-col'>
                  <Text variant='label' as='span' className='truncate'>
                    {label}
                  </Text>
                  {description && (
                    <Text
                      variant='meta'
                      className='truncate text-text-secondary'
                    >
                      {description}
                    </Text>
                  )}
                  {core_skills.length > 0 && (
                    <Text
                      variant='meta'
                      className='truncate text-text-tertiary'
                    >
                      {core_skills.join(' · ')}
                    </Text>
                  )}
                </div>
                <Button
                  name={`target-suggest-create-${i}`}
                  variant='outline'
                  size='sm'
                  onClick={() => handleCreateSuggestion(match)}
                  disabled={creatingLabel !== null}
                  className='shrink-0'
                >
                  {busy ? (
                    <>
                      <Spinner size='sm' />
                      <span>Adding…</span>
                    </>
                  ) : match.is_new ? (
                    'Create'
                  ) : (
                    'Follow'
                  )}
                </Button>
              </li>
            );
          })}
        </ul>
      </div>
    );
  };

  return (
    <div className='flex flex-col gap-4 pt-4'>
      <Input
        label='Search existing targets'
        helperText='Find a role someone already set up and follow it — no need to recreate it.'
        placeholder='e.g. frontend engineer'
        value={query}
        onChange={e => setQuery(e.target.value)}
        autoFocus
      />

      {trimmed.length < MIN_QUERY_LENGTH ? (
        <Text variant='meta' className='text-text-tertiary'>
          Type at least {MIN_QUERY_LENGTH} characters to search.
        </Text>
      ) : loading ? (
        <div className='flex items-center gap-2 py-4 text-text-secondary'>
          <Spinner size='sm' />
          <Text variant='meta'>Searching…</Text>
        </div>
      ) : results.length === 0 ? (
        searched ? (
          <div className='flex flex-col gap-3'>
            <Text variant='meta' className='text-text-secondary'>
              No targets match “{trimmed}” yet — nobody’s set this role up.
            </Text>
            <div className='flex flex-wrap items-center gap-2'>
              {renderAiFallback('primary')}
              {/* The direct path out of the dead end: the user already typed
                  the role they want, so carry it rather than making them
                  retype it into an empty Manual tab. */}
              <Button
                name='target-search-create-manually'
                variant='outline'
                size='sm'
                onClick={() => onCreateManually(trimmed)}
              >
                Create “{trimmed}” manually
              </Button>
            </div>
          </div>
        ) : null
      ) : (
        <div className='flex flex-col gap-3'>
          <ul className='flex flex-col gap-2' aria-label='Search results'>
            {results.map(target => (
              <li
                key={target.id}
                className='flex items-center justify-between gap-3 rounded-lg border border-border p-3'
              >
                <div className='flex min-w-0 flex-col'>
                  <Text variant='label' as='span' className='truncate'>
                    {target.label}
                  </Text>
                  {target.description && (
                    <Text
                      variant='meta'
                      className='truncate text-text-secondary'
                    >
                      {target.description}
                    </Text>
                  )}
                </div>
                {target.is_linked ? (
                  <Text variant='meta' className='shrink-0 text-text-tertiary'>
                    Following
                  </Text>
                ) : (
                  <Button
                    name={`target-search-follow-${target.id}`}
                    variant='outline'
                    size='sm'
                    onClick={() => handleFollow(target)}
                    disabled={followingId !== null}
                    className='shrink-0'
                  >
                    {followingId === target.id ? (
                      <>
                        <Spinner size='sm' />
                        <span>Following…</span>
                      </>
                    ) : (
                      'Follow'
                    )}
                  </Button>
                )}
              </li>
            ))}
          </ul>
          {renderAiFallback('quiet')}
        </div>
      )}
    </div>
  );
}
