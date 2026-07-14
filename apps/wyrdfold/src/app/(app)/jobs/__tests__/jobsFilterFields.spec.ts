/**
 * The shared filter-field codec — the single list that the URL state,
 * localStorage persistence, and API param mapping all derive from.
 * These tests pin the mapping tables and the one behavior that bit us
 * before the module existed: logistics filters existing in some layers
 * and not others.
 */

import {
  JOBS_FILTER_FIELDS,
  coerceStoredFilters,
  emptyFilters,
  filtersToApiParams,
  filtersToUrlPatch,
  isFilterStateEmpty,
  pickFilters,
} from '../jobsFilterFields';

const FULL = {
  search: 'react',
  status: 'new',
  minScore: '60',
  excludeLocations: 'UK',
  onlyLocations: 'US,Remote',
  remoteOnly: 'true',
  minSalary: '150000',
  country: 'US',
};

describe('JOBS_FILTER_FIELDS', () => {
  it('covers all 8 dimensions with unique keys on every axis', () => {
    expect(JOBS_FILTER_FIELDS).toHaveLength(8);
    for (const axis of ['field', 'urlKey', 'apiParam'] as const) {
      const values = JOBS_FILTER_FIELDS.map(f => f[axis]);
      expect(new Set(values).size).toBe(values.length);
    }
  });
});

describe('filtersToApiParams', () => {
  it('maps every active dimension onto its /api/jobs param', () => {
    expect(filtersToApiParams(FULL)).toEqual({
      search: 'react',
      status: 'new',
      min_score: '60',
      exclude_locations: 'UK',
      only_locations: 'US,Remote',
      remote_only: 'true',
      min_salary: '150000',
      country: 'US',
    });
  });

  it('omits inactive dimensions entirely', () => {
    expect(filtersToApiParams(emptyFilters())).toEqual({});
    expect(filtersToApiParams({ ...emptyFilters(), country: 'US' })).toEqual({
      country: 'US',
    });
  });
});

describe('filtersToUrlPatch', () => {
  it('maps empty strings to null so inactive dims leave the URL', () => {
    const patch = filtersToUrlPatch({ ...emptyFilters(), search: 'x' });
    expect(patch.search).toBe('x');
    expect(patch.status).toBeNull();
    expect(patch.remoteOnly).toBeNull();
    expect(Object.keys(patch)).toHaveLength(JOBS_FILTER_FIELDS.length);
  });
});

describe('pickFilters / isFilterStateEmpty', () => {
  it('drops navigation fields and keeps all 8 dimensions', () => {
    const picked = pickFilters({
      ...FULL,
      // superset noise a URL-state object would carry:
      sort: 'score',
      order: 'desc',
      targetId: 't-1',
    } as never);
    expect(picked).toEqual(FULL);
  });

  it('empty detection covers the logistics dimensions too', () => {
    expect(isFilterStateEmpty(emptyFilters())).toBe(true);
    // A logistics-only filter set is NOT empty — the pre-codec bug class:
    // layers that enumerated only the original five would call this bare.
    expect(isFilterStateEmpty({ ...emptyFilters(), remoteOnly: 'true' })).toBe(
      false
    );
    expect(isFilterStateEmpty({ ...emptyFilters(), minSalary: '100000' })).toBe(
      false
    );
  });
});

describe('coerceStoredFilters', () => {
  it('round-trips a full 8-field snapshot', () => {
    expect(coerceStoredFilters(JSON.parse(JSON.stringify(FULL)))).toEqual(FULL);
  });

  it('restores a legacy v1 snapshot (5 fields, pre-logistics) with the newer dimensions empty', () => {
    const v1 = {
      search: 'react',
      status: 'new',
      minScore: '60',
      excludeLocations: 'UK',
      onlyLocations: 'US',
    };
    expect(coerceStoredFilters(v1)).toEqual({
      ...v1,
      remoteOnly: '',
      minSalary: '',
      country: '',
    });
  });

  it('drops non-string fields without poisoning the snapshot', () => {
    const out = coerceStoredFilters({ search: 'x', minSalary: 150000 });
    expect(out).toEqual({ ...emptyFilters(), search: 'x' });
  });

  it('returns null for garbage or all-empty payloads', () => {
    expect(coerceStoredFilters(null)).toBeNull();
    expect(coerceStoredFilters('nope')).toBeNull();
    expect(coerceStoredFilters({})).toBeNull();
    expect(coerceStoredFilters({ search: '' })).toBeNull();
  });
});
