/**
 * @jest-environment jsdom
 */

// This file loads the module under test via `await import()` inside the tests
// (so `jest.resetModules()` can re-evaluate it per case), which leaves no
// top-level import/export. Without this marker TS treats the file as a global
// script and its `const`s collide with the sibling auth-server.test.ts.
export {};

// Typed with a rest parameter so the `jest.mock` factory below can spread its
// args through, and so `mock.calls[0]` is `unknown[]` rather than `[]`.
const mockCreateBrowserClient = jest.fn((..._args: unknown[]) => ({
  auth: { _tag: 'browser' },
}));

jest.mock('@supabase/ssr', () => ({
  createBrowserClient: (...args: unknown[]) => mockCreateBrowserClient(...args),
}));

const ENV_KEYS = ['NEXT_PUBLIC_SUPABASE_URL', 'NEXT_PUBLIC_SUPABASE_ANON_ID'];

describe('createAuthBrowserClient', () => {
  const originalEnv: Record<string, string | undefined> = {};

  beforeEach(() => {
    jest.resetModules();
    mockCreateBrowserClient.mockClear();
    for (const key of ENV_KEYS) {
      originalEnv[key] = process.env[key];
    }
  });

  afterEach(() => {
    for (const key of ENV_KEYS) {
      const value = originalEnv[key];
      if (value === undefined) {
        delete process.env[key];
      } else {
        process.env[key] = value;
      }
    }
  });

  it('returns a Supabase browser client when env vars are present', async () => {
    process.env['NEXT_PUBLIC_SUPABASE_URL'] = 'https://supabase.test';
    process.env['NEXT_PUBLIC_SUPABASE_ANON_ID'] = 'anon-key';

    const { createAuthBrowserClient } = await import('./auth-client');
    const client = createAuthBrowserClient();

    expect(mockCreateBrowserClient).toHaveBeenCalledWith(
      'https://supabase.test',
      'anon-key'
    );
    expect(client).toEqual({ auth: { _tag: 'browser' } });
  });

  it('throws when NEXT_PUBLIC_SUPABASE_URL is missing', async () => {
    delete process.env['NEXT_PUBLIC_SUPABASE_URL'];
    process.env['NEXT_PUBLIC_SUPABASE_ANON_ID'] = 'anon-key';

    const { createAuthBrowserClient } = await import('./auth-client');
    expect(() => createAuthBrowserClient()).toThrow(/NEXT_PUBLIC_SUPABASE_URL/);
    expect(mockCreateBrowserClient).not.toHaveBeenCalled();
  });

  it('throws when NEXT_PUBLIC_SUPABASE_ANON_ID is missing', async () => {
    process.env['NEXT_PUBLIC_SUPABASE_URL'] = 'https://supabase.test';
    delete process.env['NEXT_PUBLIC_SUPABASE_ANON_ID'];

    const { createAuthBrowserClient } = await import('./auth-client');
    expect(() => createAuthBrowserClient()).toThrow(
      /NEXT_PUBLIC_SUPABASE_ANON_ID/
    );
    expect(mockCreateBrowserClient).not.toHaveBeenCalled();
  });
});
