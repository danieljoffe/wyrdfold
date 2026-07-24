'use client';

import { useCallback, useState } from 'react';
import Link from 'next/link';
import { Input } from '@danieljoffe/shared-ui/Input';
import { Spinner } from '@danieljoffe/shared-ui/Spinner';
import { Text } from '@danieljoffe/shared-ui/Text';
import Button from '@/components/kit/Button';
import { extractApiError } from '@/lib/extractApiError';
import type { JobSearchResponse, JobSearchResult } from './types';

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
      <div className='flex items-start justify-between gap-3'>
        <div className='min-w-0'>
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
          {meta && (
            <Text variant='meta' className='text-text-secondary'>
              {meta}
            </Text>
          )}
        </div>
        {job.salary_text && (
          <Text variant='meta' className='shrink-0 text-text-secondary'>
            {job.salary_text}
          </Text>
        )}
      </div>
    </li>
  );
}

export default function JobSearchExplorer() {
  const [draft, setDraft] = useState('');
  const [query, setQuery] = useState(''); // the last SUBMITTED query
  const [results, setResults] = useState<JobSearchResult[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const runSearch = useCallback(async (raw: string) => {
    const q = raw.trim();
    if (!q) return;
    setLoading(true);
    setError(null);
    setQuery(q);
    try {
      const res = await fetch(`/api/jobs/search?q=${encodeURIComponent(q)}`);
      if (!res.ok) {
        setError(await extractApiError(res, 'Search failed'));
        setResults(null);
        return;
      }
      const data = (await res.json()) as JobSearchResponse;
      setResults(data.results);
    } catch {
      setError('Network error running search.');
      setResults(null);
    } finally {
      setLoading(false);
    }
  }, []);

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
            <ul className='space-y-3'>
              {results.map(job => (
                <JobSearchRow key={job.id} job={job} />
              ))}
            </ul>
          )}
        </section>
      )}
    </div>
  );
}
