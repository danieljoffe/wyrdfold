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

  afterEach(() => {
    if (origSha === undefined) delete process.env['NEXT_PUBLIC_BUILD_SHA'];
    else process.env['NEXT_PUBLIC_BUILD_SHA'] = origSha;
    if (origVercelEnv === undefined) delete process.env['VERCEL_ENV'];
    else process.env['VERCEL_ENV'] = origVercelEnv;
  });

  it('reports the SHA injected at deploy time', async () => {
    process.env['NEXT_PUBLIC_BUILD_SHA'] = '71e0af47deadbeef';
    process.env['VERCEL_ENV'] = 'production';
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
    delete process.env['VERCEL_ENV'];
    const res = GET();
    expect(await res.json()).toEqual({ commit: null, environment: null });
  });
});
