/**
 * @jest-environment node
 */
import { clientIpFromHeaders, createRateLimiter } from '@/lib/rateLimit';

describe('createRateLimiter', () => {
  beforeEach(() => {
    jest.useFakeTimers();
    jest.setSystemTime(new Date('2026-06-23T00:00:00.000Z'));
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  it('allows up to `limit` requests then blocks within the window', () => {
    const rl = createRateLimiter({ limit: 3, windowMs: 60_000 });

    expect(rl.check('ip').allowed).toBe(true);
    expect(rl.check('ip').allowed).toBe(true);
    const third = rl.check('ip');
    expect(third.allowed).toBe(true);
    expect(third.remaining).toBe(0);

    const fourth = rl.check('ip');
    expect(fourth.allowed).toBe(false);
    expect(fourth.remaining).toBe(0);
  });

  it('resets the budget after the window lapses', () => {
    const rl = createRateLimiter({ limit: 1, windowMs: 60_000 });

    expect(rl.check('ip').allowed).toBe(true);
    expect(rl.check('ip').allowed).toBe(false);

    jest.advanceTimersByTime(60_001);

    expect(rl.check('ip').allowed).toBe(true);
  });

  it('tracks separate budgets per key', () => {
    const rl = createRateLimiter({ limit: 1, windowMs: 60_000 });

    expect(rl.check('a').allowed).toBe(true);
    expect(rl.check('a').allowed).toBe(false);
    // A different key is unaffected.
    expect(rl.check('b').allowed).toBe(true);
  });

  it('reports a resetAt in the future while blocked', () => {
    const rl = createRateLimiter({ limit: 1, windowMs: 60_000 });
    rl.check('ip');
    const blocked = rl.check('ip');
    expect(blocked.resetAt).toBeGreaterThan(Date.now());
  });
});

describe('clientIpFromHeaders', () => {
  it('takes the first IP from x-forwarded-for', () => {
    const h = new Headers({ 'x-forwarded-for': '1.2.3.4, 5.6.7.8' });
    expect(clientIpFromHeaders(h)).toBe('1.2.3.4');
  });

  it('falls back to x-real-ip', () => {
    const h = new Headers({ 'x-real-ip': '9.9.9.9' });
    expect(clientIpFromHeaders(h)).toBe('9.9.9.9');
  });

  it('falls back to a constant when no IP header is present', () => {
    expect(clientIpFromHeaders(new Headers())).toBe('unknown');
  });
});
