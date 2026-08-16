import { describeAddJobFailure } from '../addJobFailure';

describe('describeAddJobFailure', () => {
  // The LinkedIn case: the page 404s our direct fetch and Firecrawl gets a 403.
  it('names the block and redirects to the ATS link for 403/401/429', () => {
    for (const w of [
      ['firecrawl_failed:http_403'],
      ['http_status:403'],
      ['http_status:401'],
      ['http_status:429'],
      ['http_status:404', 'firecrawl_failed:http_403', 'fetch_non_200'],
    ]) {
      expect(describeAddJobFailure(w)).toMatch(/blocks automated readers/i);
      expect(describeAddJobFailure(w)).toMatch(/Greenhouse/);
    }
  });

  it('calls out a plain 404 as a possibly-removed posting', () => {
    expect(describeAddJobFailure(['http_status:404', 'fetch_non_200'])).toMatch(
      /404/
    );
  });

  it('suggests retrying on a 5xx', () => {
    expect(describeAddJobFailure(['http_status:503'])).toMatch(/server error/i);
  });

  it('explains a reachable page with no posting on it', () => {
    expect(describeAddJobFailure(['firecrawl_failed:no_metadata'])).toMatch(
      /couldn't find a job posting/i
    );
    expect(describeAddJobFailure(['firecrawl_failed:empty_html'])).toMatch(
      /couldn't find a job posting/i
    );
  });

  it('handles redirect and reachability warnings', () => {
    expect(describeAddJobFailure(['too_many_redirects'])).toMatch(
      /redirected/i
    );
    expect(describeAddJobFailure(['fetch_failed'])).toMatch(/couldn't reach/i);
    expect(
      describeAddJobFailure(['content_verification:not_a_job_posting'])
    ).toMatch(/doesn't look like a job posting/i);
  });

  it('falls back to a generic line rather than leaking raw warning tokens', () => {
    const msg = describeAddJobFailure(['something_new_we_dont_map', '']);
    expect(msg).toMatch(/couldn't read a job posting/i);
    expect(msg).not.toMatch(/something_new_we_dont_map/);
  });

  it('is safe on an empty warning list', () => {
    expect(describeAddJobFailure([])).toMatch(/couldn't read a job posting/i);
  });

  // 403 must win over a co-occurring 404: the 404 is our direct fetch being
  // refused, the 403 is the reason, and only the 403 message is actionable.
  it('prefers the blocked-reader message when both appear', () => {
    expect(
      describeAddJobFailure(['http_status:404', 'firecrawl_failed:http_403'])
    ).toMatch(/blocks automated readers/i);
  });
});
