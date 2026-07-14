import { renderHook } from '@testing-library/react';

import { emptyFilters } from '../jobsFilterFields';
import type { JobsFilterState } from '../types';
import { useJobsFilterPersistence } from '../useJobsFilterPersistence';

const EMPTY: JobsFilterState = emptyFilters();

const POPULATED: JobsFilterState = {
  ...emptyFilters(),
  search: 'react',
  status: 'new',
  minScore: '60',
  excludeLocations: 'UK',
  onlyLocations: 'US',
  remoteOnly: 'true',
  minSalary: '150000',
  country: 'US',
};

describe('useJobsFilterPersistence', () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it('round-trips a populated snapshot keyed by target', () => {
    const { result } = renderHook(() => useJobsFilterPersistence());
    result.current.write('target-1', POPULATED);

    expect(result.current.read('target-1')).toEqual(POPULATED);
  });

  it('uses the __all__ sentinel for undefined targets', () => {
    const { result } = renderHook(() => useJobsFilterPersistence());
    result.current.write(undefined, POPULATED);

    // Snapshot stored under the All Jobs key, isolated from per-target entries.
    expect(window.localStorage.getItem('wyrdfold.filters.__all__')).toContain(
      'react'
    );
    expect(result.current.read('target-1')).toBeNull();
    expect(result.current.read(undefined)).toEqual(POPULATED);
  });

  it('returns null for a missing target', () => {
    const { result } = renderHook(() => useJobsFilterPersistence());

    expect(result.current.read('never-seen')).toBeNull();
  });

  it('returns null when the stored snapshot has no populated fields', () => {
    // Defensive: a stale "all empty" entry shouldn't trigger a restore
    // that overwrites a deep link with nothing.
    window.localStorage.setItem('wyrdfold.filters.t', JSON.stringify(EMPTY));
    const { result } = renderHook(() => useJobsFilterPersistence());

    expect(result.current.read('t')).toBeNull();
  });

  it('write removes the entry when all fields are empty', () => {
    window.localStorage.setItem(
      'wyrdfold.filters.t',
      JSON.stringify(POPULATED)
    );
    const { result } = renderHook(() => useJobsFilterPersistence());
    result.current.write('t', EMPTY);

    // Clearing all filters should NOT leave a stale snapshot that
    // re-applies on the next visit.
    expect(window.localStorage.getItem('wyrdfold.filters.t')).toBeNull();
  });

  it('survives malformed JSON in storage', () => {
    window.localStorage.setItem('wyrdfold.filters.t', '{not valid json');
    const { result } = renderHook(() => useJobsFilterPersistence());

    expect(result.current.read('t')).toBeNull();
  });

  it('drops non-string fields on read (forward-compat)', () => {
    // A future version could store a number / array / object for a
    // field we haven't taught the coerce step about — fall through to
    // empty string for that field rather than throwing.
    window.localStorage.setItem(
      'wyrdfold.filters.t',
      JSON.stringify({
        search: 'react',
        status: 42, // wrong type
        minScore: ['junk'], // wrong type
        excludeLocations: 'UK',
        onlyLocations: 'US',
      })
    );
    const { result } = renderHook(() => useJobsFilterPersistence());

    expect(result.current.read('t')).toEqual({
      ...emptyFilters(),
      search: 'react',
      excludeLocations: 'UK',
      onlyLocations: 'US',
    });
  });

  it('restores a pre-logistics v1 snapshot with the newer dimensions empty', () => {
    // Snapshots written before the logistics filters joined persistence
    // hold only the original five fields. They must keep restoring those
    // five — with remote/salary/country simply inactive, not poisoned.
    window.localStorage.setItem(
      'wyrdfold.filters.t-legacy',
      JSON.stringify({
        search: 'react',
        status: 'new',
        minScore: '60',
        excludeLocations: 'UK',
        onlyLocations: 'US',
      })
    );
    const { result } = renderHook(() => useJobsFilterPersistence());
    expect(result.current.read('t-legacy')).toEqual({
      search: 'react',
      status: 'new',
      minScore: '60',
      excludeLocations: 'UK',
      onlyLocations: 'US',
      remoteOnly: '',
      minSalary: '',
      country: '',
    });
  });

  it('clear removes the entry', () => {
    const { result } = renderHook(() => useJobsFilterPersistence());
    result.current.write('t', POPULATED);
    result.current.clear('t');

    expect(result.current.read('t')).toBeNull();
  });
});
