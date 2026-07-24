'use client';

import { useCallback, useState } from 'react';
import Link from 'next/link';
import { Input } from '@danieljoffe/shared-ui/Input';
import { Spinner } from '@danieljoffe/shared-ui/Spinner';
import { Text } from '@danieljoffe/shared-ui/Text';
import Button from '@/components/kit/Button';
import { extractApiError } from '@/lib/extractApiError';
import { timeAgo } from '@/lib/timeAgo';
import type { JobSearchResponse, JobSearchResult } from './types';

const PAGE_SIZE = 20;

/** Two-letter monogram from a company name. */
function initials(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return '?';
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[words.length - 1][0]).toUpperCase();
}

/** Deterministic hue per company so the same company is always the same colour —
 *  a stable visual anchor for skimming (we don't have real logos: no company
 *  domain is stored, and `absolute_url` is the ATS board, not the company site). */
function hueFor(name: string): number {
  let h = 0;
  for (let i = 0; i < name.length; i += 1)
    h = (h * 31 + name.charCodeAt(i)) % 360;
  return h;
}

function CompanyAvatar({ name }: { name: string }) {
  // Solid colour + white text → self-contained, reads in light and dark themes.
  return (
    <span
      aria-hidden
      className='flex size-9 shrink-0 items-center justify-center rounded-md text-xs font-semibold text-white'
      style={{ backgroundColor: `hsl(${hueFor(name)} 48% 42%)` }}
    >
      {initials(name)}
    </span>
  );
}

/**
 * Manual keyword job search (#467). Kept deliberately DISTINCT from the matched
 * Jobs view: no match scores here (results aren't ranked against the user's
 * profile), so search is never mistaken for the quality of the matching engine —
 * and "see your matches" stays the reason to use Jobs. Logged-in only.
 */
function JobSearchRow({ job }: { job: JobSearchResult }) {
  const meta = [job.company_name, job.location].filter(Boolean).join(' · ');
  return (
    <li className='rounded-md border border-border bg-surface-tertiary p-3'>
      <div className='flex items-start gap-3'>
        <CompanyAvatar name={job.company_name} />
        <div className='min-w-0 flex-1'>
          {/* Line 1: role + salary */}
          <div className='flex items-start justify-between gap-3'>
            {job.absolute_url ? (
              <a
                href={job.absolute_url}
                target='_blank'
                rel='noopener noreferrer'
                className='font-medium underline underline-offset-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2'
              >
                {job.title}
              </a>
            ) : (
              <span className='font-medium'>{job.title}</span>
            )}
            {job.salary_text && (
              <Text variant='meta' className='shrink-0 text-text-secondary'>
                {job.salary_text}
              </Text>
            )}
          </div>
          {/* Line 2: company · location + posted date */}
          <div className='mt-0.5 flex items-baseline justify-between gap-3'>
            {meta && (
              <Text variant='meta' className='truncate text-text-secondary'>
                {meta}
              </Text>
            )}
            <Text variant='meta' className='shrink-0 text-text-tertiary'>
              {timeAgo(job.created_at)}
            </Text>
          </div>
        </div>
      </div>
    </li>
  );
}

export default function JobSearchExplorer() {
  const [draft, setDraft] = useState('');
  const [query, setQuery] = useState(''); // the last SUBMITTED query
  const [results, setResults] = useState<JobSearchResult[] | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false); // fresh search
  const [loadingMore, setLoadingMore] = useState(false); // "Load more"
  const [error, setError] = useState<string | null>(null);

  const fetchPage = useCallback(async (q: string, offset: number) => {
    const res = await fetch(
      `/api/jobs/search?q=${encodeURIComponent(q)}&page_size=${PAGE_SIZE}&offset=${offset}`
    );
    if (!res.ok) throw new Error(await extractApiError(res, 'Search failed'));
    return (await res.json()) as JobSearchResponse;
  }, []);

  const runSearch = useCallback(
    async (raw: string) => {
      const q = raw.trim();
      if (!q) return;
      setLoading(true);
      setError(null);
      setQuery(q);
      setResults(null);
      try {
        const data = await fetchPage(q, 0);
        setResults(data.results);
        setHasMore(data.has_more);
      } catch (e) {
        setError(
          e instanceof Error ? e.message : 'Network error running search.'
        );
        setResults(null);
      } finally {
        setLoading(false);
      }
    },
    [fetchPage]
  );

  const loadMore = useCallback(async () => {
    if (!results) return;
    setLoadingMore(true);
    setError(null);
    try {
      const data = await fetchPage(query, results.length);
      setResults(prev => [...(prev ?? []), ...data.results]);
      setHasMore(data.has_more);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Network error loading more.');
    } finally {
      setLoadingMore(false);
    }
  }, [fetchPage, query, results]);

  return (
    <div className='mx-auto max-w-3xl space-y-6'>
      <header className='space-y-1'>
        <h1 className='text-2xl font-semibold text-text-primary'>
          Search jobs
        </h1>
        <Text variant='body' className='text-text-secondary'>
          Browse the full job pool by keyword. These results aren’t scored
          against your profile — head to{' '}
          <Link href='/jobs' className='underline underline-offset-2'>
            Jobs
          </Link>{' '}
          to see how you match.
        </Text>
      </header>

      <div className='flex gap-2'>
        <div className='flex-1'>
          <Input
            value={draft}
            onChange={e => setDraft(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter') {
                e.preventDefault();
                runSearch(draft);
              }
            }}
            placeholder='e.g. senior frontend engineer'
            aria-label='Search jobs by title or keyword'
          />
        </div>
        <Button
          name='job-search-submit'
          onClick={() => runSearch(draft)}
          disabled={loading || !draft.trim()}
        >
          Search
        </Button>
      </div>

      {loading && (
        <div
          className='flex items-center gap-2'
          role='status'
          aria-live='polite'
        >
          <Spinner size='sm' aria-label='Searching' />
          <Text variant='meta'>Searching…</Text>
        </div>
      )}

      {error && !loading && (
        <Text variant='error' role='alert'>
          {error}
        </Text>
      )}

      {!loading && !error && results !== null && (
        <section aria-live='polite'>
          {results.length === 0 ? (
            <div className='space-y-1'>
              <Text variant='body'>No roles match “{query}” yet.</Text>
              <Text variant='meta' className='text-text-secondary'>
                We’re expanding coverage — try a broader title, or check back
                soon.
              </Text>
            </div>
          ) : (
            <>
              <ul className='space-y-3'>
                {results.map(job => (
                  <JobSearchRow key={job.id} job={job} />
                ))}
              </ul>
              {hasMore && (
                <div className='mt-4 flex justify-center'>
                  <Button
                    name='job-search-load-more'
                    variant='secondary'
                    onClick={loadMore}
                    disabled={loadingMore}
                    aria-busy={loadingMore}
                  >
                    {loadingMore ? 'Loading…' : 'Load more'}
                  </Button>
                </div>
              )}
            </>
          )}
        </section>
      )}
    </div>
  );
}
