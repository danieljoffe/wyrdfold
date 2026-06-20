import {
  THEME_COOKIE,
  THEME_RESOLVED_COOKIE,
  hasThemeCookie,
  resolveIsDark,
  writeThemeCookies,
} from '../themeCookies';

function clearCookies(): void {
  for (const c of document.cookie.split(';')) {
    const name = c.split('=')[0]?.trim();
    if (name) document.cookie = `${name}=; max-age=0; path=/`;
  }
}

describe('resolveIsDark', () => {
  it('honors an explicit dark/light preference regardless of resolved', () => {
    expect(resolveIsDark('dark', 'light')).toBe(true);
    expect(resolveIsDark('light', 'dark')).toBe(false);
  });

  it('falls back to the cached resolved value for system', () => {
    expect(resolveIsDark('system', 'dark')).toBe(true);
    expect(resolveIsDark('system', 'light')).toBe(false);
    expect(resolveIsDark('system', undefined)).toBe(false);
  });

  it('defaults to light for an unset or garbage preference', () => {
    expect(resolveIsDark(undefined, undefined)).toBe(false);
    expect(resolveIsDark('nonsense', 'dark')).toBe(true); // resolved still wins
    expect(resolveIsDark('nonsense', undefined)).toBe(false);
  });
});

describe('writeThemeCookies / hasThemeCookie', () => {
  beforeEach(clearCookies);

  it('reports no cookie before any write', () => {
    expect(hasThemeCookie()).toBe(false);
  });

  it('persists preference and resolved value, then detects the cookie', () => {
    writeThemeCookies('dark', 'dark');
    expect(document.cookie).toContain(`${THEME_COOKIE}=dark`);
    expect(document.cookie).toContain(`${THEME_RESOLVED_COOKIE}=dark`);
    expect(hasThemeCookie()).toBe(true);
  });
});
