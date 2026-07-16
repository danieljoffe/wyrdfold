'use client';

import { useCallback, useEffect, useState } from 'react';
import { Input } from '@danieljoffe/shared-ui/Input';
import { Spinner } from '@danieljoffe/shared-ui/Spinner';
import { Text } from '@danieljoffe/shared-ui/Text';
import Button from '@/components/kit/Button';
import type { TargetSearchResult } from './types';

const MIN_QUERY_LENGTH = 2;
const DEBOUNCE_MS = 300;

interface TargetSearchTabProps {
  /**
   * Follow (link) a discovered target. Resolves `true` when the link
   * succeeded so the row can flip to "Following", or `false` when it failed
   * (e.g. the active-target limit) — the parent owns the toast either way.
   */
  onFollow: (target: TargetSearchResult) => Promise<boolean>;
}

/**
 * "Search existing targets" — discovery over the shared catalog. Targets are
 * shared, so a role another user already created can be followed instead of
 * recreated. Debounced substring search hits `GET /api/targets/search`; each
 * result can be followed inline (the parent links + refreshes the list).
 */
export default function TargetSearchTab({ onFollow }: TargetSearchTabProps) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<TargetSearchResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [searched, setSearched] = useState(false);
  const [followingId, setFollowingId] = useState<string | null>(null);

  const trimmed = query.trim();

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
          <Text variant='meta' className='text-text-secondary'>
            No targets match “{trimmed}”. Use the Manual or From URL tab to
            create one.
          </Text>
        ) : null
      ) : (
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
                  <Text variant='meta' className='truncate text-text-secondary'>
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
      )}
    </div>
  );
}
