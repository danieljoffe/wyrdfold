'use client';

import {
  type ReactNode,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react';
import {
  type ResolvedTheme,
  type ThemePreference,
  hasThemeCookie,
  writeThemeCookies,
} from './themeCookies';

type Theme = ThemePreference;

interface ThemeContextType {
  theme: Theme;
  resolvedTheme: ResolvedTheme;
  isDarkMode: boolean;
  setTheme: (theme: Theme) => void;
  toggleDarkMode: () => void;
}

const ThemeContext = createContext<ThemeContextType>({
  theme: 'system',
  resolvedTheme: 'light',
  isDarkMode: false,
  setTheme: () => undefined,
  toggleDarkMode: () => undefined,
});

export function useTheme(): ThemeContextType {
  return useContext(ThemeContext);
}

const THEME_STORAGE_KEY = 'theme';

function getSystemPrefersDark(): boolean {
  if (typeof window === 'undefined') return false;
  return window.matchMedia('(prefers-color-scheme: dark)').matches;
}

function getStoredTheme(): Theme {
  if (typeof window === 'undefined') return 'system';
  const stored = localStorage.getItem(THEME_STORAGE_KEY);
  if (stored === 'light' || stored === 'dark' || stored === 'system') {
    return stored;
  }
  return 'system';
}

interface ThemeProviderProps {
  children: ReactNode;
  /** Preference read from the `theme` cookie server-side (defaults to system). */
  initialTheme?: Theme;
  /**
   * Concrete value the server painted onto `<html>` (from `theme-resolved`).
   * Seeds client state so the first render matches the server — no flash, no
   * hydration mismatch.
   */
  initialResolvedTheme?: ResolvedTheme;
}

export function ThemeProvider({
  children,
  initialTheme = 'system',
  initialResolvedTheme = 'light',
}: ThemeProviderProps) {
  const [theme, _setTheme] = useState<Theme>(initialTheme);
  // Seed from the server-resolved value so `isDarkMode` on the first client
  // render equals the class the server already painted. For an explicit
  // light/dark preference this is moot (isDarkMode derives from `theme`); it
  // only carries the `system` resolution the server couldn't compute itself.
  const [systemPrefersDark, setSystemPrefersDark] = useState(
    initialResolvedTheme === 'dark'
  );

  // After hydration, sync the live OS preference and migrate users who set a
  // preference before cookies existed (localStorage-only). Skipped for anyone
  // already on a cookie, so it never fights the server-seeded state. All
  // effect-driven, so it can't produce a hydration warning.
  useEffect(() => {
    setSystemPrefersDark(getSystemPrefersDark());
    if (!hasThemeCookie()) {
      const stored = getStoredTheme();
      _setTheme(prev => (stored === prev ? prev : stored));
    }
  }, []);

  useEffect(() => {
    const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
    const handleChange = (e: MediaQueryListEvent) => {
      setSystemPrefersDark(e.matches);
    };
    mediaQuery.addEventListener('change', handleChange);
    return () => mediaQuery.removeEventListener('change', handleChange);
  }, []);

  const isDarkMode = useMemo(() => {
    if (theme === 'system') return systemPrefersDark;
    return theme === 'dark';
  }, [theme, systemPrefersDark]);

  const resolvedTheme: ResolvedTheme = isDarkMode ? 'dark' : 'light';

  useEffect(() => {
    document.documentElement.classList.toggle('dark', isDarkMode);
  }, [isDarkMode]);

  // Persist preference + resolved value so the next server render paints the
  // correct class with no inline script. Runs on mount (migrating pre-cookie
  // users) and on every change (user toggle, OS switch).
  useEffect(() => {
    writeThemeCookies(theme, resolvedTheme);
  }, [theme, resolvedTheme]);

  const setTheme = useCallback((mode: Theme) => {
    _setTheme(mode);
    localStorage.setItem(THEME_STORAGE_KEY, mode);
  }, []);

  const toggleDarkMode = useCallback(() => {
    const newMode = isDarkMode ? 'light' : 'dark';
    setTheme(newMode);
  }, [isDarkMode, setTheme]);

  const value = useMemo(
    () => ({ theme, resolvedTheme, isDarkMode, setTheme, toggleDarkMode }),
    [theme, resolvedTheme, isDarkMode, setTheme, toggleDarkMode]
  );

  return (
    <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
  );
}
