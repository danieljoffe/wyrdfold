/**
 * @jest-environment node
 */
import { GET } from './route';

// The release gate's FE provenance check (#976): the post-deploy smoke
// asserts this route's `commit` equals the release HEAD, the same way it
// asserts the API's `/version`. In the built artifact the env reference is
// inlined at build time from `--build-env NEXT_PUBLIC_BUILD_SHA=...`; jest
// reads it at runtime, which is what lets these tests drive both states.
describe('GET /api/version (build provenance)', () => {
  const origSha = process.env['NEXT_PUBLIC_BUILD_SHA'];
  const origVercelEnv = process.env['VERCEL_ENV'];
  const origEnvName = process.env['NEXT_PUBLIC_ENV_NAME'];

  afterEach(() => {
    if (origSha === undefined) delete process.env['NEXT_PUBLIC_BUILD_SHA'];
    else process.env['NEXT_PUBLIC_BUILD_SHA'] = origSha;
    if (origVercelEnv === undefined) delete process.env['VERCEL_ENV'];
    else process.env['VERCEL_ENV'] = origVercelEnv;
    if (origEnvName === undefined) delete process.env['NEXT_PUBLIC_ENV_NAME'];
    else process.env['NEXT_PUBLIC_ENV_NAME'] = origEnvName;
  });

  it('reports the SHA injected at deploy time', async () => {
    process.env['NEXT_PUBLIC_BUILD_SHA'] = '71e0af47deadbeef';
    process.env['NEXT_PUBLIC_ENV_NAME'] = 'production';
    const res = GET();
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({
      commit: '71e0af47deadbeef',
      environment: 'production',
    });
  });

  it('reports null — never a stale or guessed value — when no SHA was injected', async () => {
    // A deploy that skipped the --build-env flag must FAIL the smoke's
    // provenance check, not pass it with a fabricated commit.
    delete process.env['NEXT_PUBLIC_BUILD_SHA'];
    delete process.env['NEXT_PUBLIC_ENV_NAME'];
    const res = GET();
    expect(await res.json()).toEqual({ commit: null, environment: null });
  });

  it('names the environment from NEXT_PUBLIC_ENV_NAME, not VERCEL_ENV', async () => {
    // The bug (#1053): VERCEL_ENV is 'production' for the production deployment
    // of ANY project, so the STAGING frontend reported environment=production.
    process.env['NEXT_PUBLIC_BUILD_SHA'] = 'abc123';
    process.env['NEXT_PUBLIC_ENV_NAME'] = 'staging';
    process.env['VERCEL_ENV'] = 'production';
    expect(await GET().json()).toEqual({
      commit: 'abc123',
      environment: 'staging',
    });
  });

  it('does NOT fall back to VERCEL_ENV when the name is unset', async () => {
    // Falling back would reinstate the wrong answer exactly when the variable
    // is missing. A wrong label is worse than none, because it is believed.
    process.env['NEXT_PUBLIC_BUILD_SHA'] = 'abc123';
    delete process.env['NEXT_PUBLIC_ENV_NAME'];
    process.env['VERCEL_ENV'] = 'production';
    expect(await GET().json()).toEqual({ commit: 'abc123', environment: null });
  });

  it('keeps commit and environment independent', async () => {
    // The release gate asserts on `commit`; a missing env name must not take
    // provenance down with it.
    process.env['NEXT_PUBLIC_BUILD_SHA'] = 'deadbeef';
    delete process.env['NEXT_PUBLIC_ENV_NAME'];
    expect((await GET().json()).commit).toBe('deadbeef');
  });
});
