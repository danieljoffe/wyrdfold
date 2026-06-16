/**
 * @jest-environment node
 */
import { NextRequest } from 'next/server';
import { proxy } from './proxy';

const URL_VAR = 'NEXT_PUBLIC_SUPABASE_URL';
const ANON_VAR = 'NEXT_PUBLIC_SUPABASE_ANON_ID';

function setEnv(name: string, value: string | undefined): void {
  if (value === undefined) {
    delete process.env[name];
  } else {
    process.env[name] = value;
  }
}

describe('proxy middleware: missing Supabase configuration', () => {
  const original: Record<string, string | undefined> = {};

  beforeEach(() => {
    original[URL_VAR] = process.env[URL_VAR];
    original[ANON_VAR] = process.env[ANON_VAR];
    original['NODE_ENV'] = process.env.NODE_ENV;
  });

  afterEach(() => {
    setEnv(URL_VAR, original[URL_VAR]);
    setEnv(ANON_VAR, original[ANON_VAR]);
    setEnv('NODE_ENV', original['NODE_ENV']);
  });

  it('returns 503 (not 401) when both Supabase vars are absent', async () => {
    setEnv(URL_VAR, undefined);
    setEnv(ANON_VAR, undefined);

    const res = await proxy(new NextRequest('http://localhost:3100/dashboard'));

    expect(res.status).toBe(503);
  });

  it('names every missing var and the remedy in development', async () => {
    setEnv(URL_VAR, undefined);
    setEnv(ANON_VAR, undefined);

    const res = await proxy(new NextRequest('http://localhost:3100/dashboard'));
    const body = await res.text();

    expect(body).toContain(URL_VAR);
    expect(body).toContain(ANON_VAR);
    expect(body).toContain('.env.local');
  });

  it('lists only the var that is actually missing', async () => {
    setEnv(URL_VAR, 'http://127.0.0.1:54321');
    setEnv(ANON_VAR, undefined);

    const res = await proxy(new NextRequest('http://localhost:3100/dashboard'));
    const body = await res.text();

    expect(body).toContain(ANON_VAR);
    expect(body).not.toContain(URL_VAR);
  });

  it('stays terse in production so a misconfigured deploy leaks nothing', async () => {
    setEnv(URL_VAR, undefined);
    setEnv(ANON_VAR, undefined);
    setEnv('NODE_ENV', 'production');

    const res = await proxy(new NextRequest('http://localhost:3100/dashboard'));
    const body = await res.text();

    expect(res.status).toBe(503);
    expect(body).not.toContain(URL_VAR);
    expect(body).not.toContain(ANON_VAR);
  });
});
