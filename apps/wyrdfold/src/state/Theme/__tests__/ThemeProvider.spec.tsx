import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';

import { ThemeProvider, useTheme } from '../ThemeProvider';
import { THEME_COOKIE, THEME_RESOLVED_COOKIE } from '../themeCookies';

function mockMatchMedia(prefersDark: boolean): void {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: (query: string) => ({
      matches: prefersDark,
      media: query,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      addListener: () => undefined,
      removeListener: () => undefined,
      dispatchEvent: () => false,
      onchange: null,
    }),
  });
}

function clearCookies(): void {
  for (const c of document.cookie.split(';')) {
    const name = c.split('=')[0]?.trim();
    if (name) document.cookie = `${name}=; max-age=0; path=/`;
  }
}

function Probe() {
  const { theme, resolvedTheme, isDarkMode } = useTheme();
  return (
    <div
      data-testid='probe'
      data-theme={theme}
      data-resolved={resolvedTheme}
      data-dark={String(isDarkMode)}
    />
  );
}

describe('ThemeProvider', () => {
  beforeEach(() => {
    clearCookies();
    localStorage.clear();
    document.documentElement.className = 'pyre';
    mockMatchMedia(false);
  });

  it('seeds state from the server-resolved props so the first render matches the paint', () => {
    // Cookie present → migration path is skipped; init comes straight from props.
    document.cookie = `${THEME_COOKIE}=dark`;
    render(
      <ThemeProvider initialTheme='dark' initialResolvedTheme='dark'>
        <Probe />
      </ThemeProvider>
    );
    const probe = screen.getByTestId('probe');
    expect(probe).toHaveAttribute('data-theme', 'dark');
    expect(probe).toHaveAttribute('data-resolved', 'dark');
    expect(probe).toHaveAttribute('data-dark', 'true');
  });

  it('applies the dark class and persists cookies after mount', async () => {
    document.cookie = `${THEME_COOKIE}=dark`;
    render(
      <ThemeProvider initialTheme='dark' initialResolvedTheme='dark'>
        <Probe />
      </ThemeProvider>
    );
    await waitFor(() => {
      expect(document.documentElement).toHaveClass('dark');
      expect(document.cookie).toContain(`${THEME_COOKIE}=dark`);
      expect(document.cookie).toContain(`${THEME_RESOLVED_COOKIE}=dark`);
    });
  });

  it('migrates a pre-cookie user from localStorage on mount', async () => {
    // No cookie, but a legacy localStorage preference exists.
    localStorage.setItem('theme', 'dark');
    render(
      <ThemeProvider initialTheme='system' initialResolvedTheme='light'>
        <Probe />
      </ThemeProvider>
    );
    await waitFor(() => {
      expect(screen.getByTestId('probe')).toHaveAttribute('data-theme', 'dark');
      expect(document.cookie).toContain(`${THEME_COOKIE}=dark`);
    });
  });

  it('resolves system to the OS preference', async () => {
    mockMatchMedia(true); // OS prefers dark
    render(
      <ThemeProvider initialTheme='system' initialResolvedTheme='dark'>
        <Probe />
      </ThemeProvider>
    );
    await waitFor(() => {
      expect(screen.getByTestId('probe')).toHaveAttribute('data-dark', 'true');
    });
  });
});
