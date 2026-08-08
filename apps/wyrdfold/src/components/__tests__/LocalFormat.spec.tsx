import fs from 'node:fs';
import path from 'node:path';
import type { ReactElement } from 'react';
import { render } from '@testing-library/react';
import {
  LocalDate,
  LocalDateTime,
  LocalNumber,
  formatLocalDateTime,
} from '../LocalFormat';

/**
 * Regression cover for the 2026-08-08 prod defect: `/targets` threw React #418
 * ("text content does not match server-rendered HTML") on every load because
 * `TargetCard` rendered `new Date(updated_at).toLocaleDateString()`, which
 * resolves against the *host's* locale and timezone — UTC on the server, the
 * user's own settings in the browser. Measured on the live row:
 * server `"8/8/2026"` vs a US-West browser `"8/7/2026"`.
 *
 * NOTE ON HOW THIS IS ASSERTED: React consumes `suppressHydrationWarning`
 * internally and does NOT emit it as a DOM attribute, so it is invisible to
 * `container.querySelector(...).getAttribute(...)` — a DOM-level assertion here
 * would pass whether or not the prop was set, and certify nothing. These
 * components are pure and hook-free, so calling them directly and inspecting
 * the returned element's props is the assertion that can actually fail.
 */

describe('LocalFormat components', () => {
  it.each([
    ['LocalDate', () => LocalDate({ value: '2026-08-08T06:50:44Z' })],
    ['LocalDateTime', () => LocalDateTime({ value: '2026-08-08T06:50:44Z' })],
    ['LocalNumber', () => LocalNumber({ value: 1234567 })],
  ])('%s marks its output hydration-exempt', (_name, make) => {
    const el = make() as ReactElement<{ suppressHydrationWarning?: boolean }>;
    expect(el.props.suppressHydrationWarning).toBe(true);
  });

  it('renders the date text', () => {
    const { container } = render(<LocalDate value='2026-08-08T06:50:44Z' />);
    expect(container.textContent).toMatch(/2026/);
  });

  it('renders a grouped number', () => {
    const { container } = render(<LocalNumber value={1234567} />);
    // The separator is locale-dependent by design, so assert on the digits.
    expect(container.textContent?.replace(/\D/g, '')).toBe('1234567');
  });

  it.each([null, undefined, '', 'not-a-date'])(
    'falls back rather than rendering "Invalid Date" for %p',
    value => {
      const { container } = render(
        <LocalDate value={value as string | null} fallback='—' />
      );
      expect(container.textContent).toBe('—');
    }
  );

  it('falls back for a non-finite number', () => {
    const { container } = render(
      <LocalNumber value={Number.NaN} fallback='n/a' />
    );
    expect(container.textContent).toBe('n/a');
  });

  it('honours explicit Intl options', () => {
    const { container } = render(
      <LocalDate
        value='2026-08-08T06:50:44Z'
        options={{ year: 'numeric', month: 'short', timeZone: 'UTC' }}
      />
    );
    expect(container.textContent).toBe('Aug 2026');
  });

  it('formatLocalDateTime returns a string for attribute use', () => {
    expect(formatLocalDateTime('2026-08-08T06:50:44Z')).toMatch(/2026/);
    expect(formatLocalDateTime(null)).toBe('');
  });
});

describe('no bare toLocale* in rendered components', () => {
  /**
   * A bare `toLocaleDateString` / `toLocaleString` in render output IS the
   * defect. Scanning is the only way to hold this: a unit test cannot observe
   * a mismatch that only appears when real SSR output meets a real browser in
   * a different timezone.
   */
  const SRC = path.join(__dirname, '..', '..');
  const ALLOWED = [path.join('components', 'LocalFormat.tsx')];

  function walk(dir: string, out: string[] = []): string[] {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const p = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        if (entry.name === '__tests__' || entry.name === 'node_modules')
          continue;
        walk(p, out);
      } else if (/\.tsx$/.test(entry.name)) {
        out.push(p);
      }
    }
    return out;
  }

  it('every .tsx uses the LocalFormat helpers instead', () => {
    const offenders: string[] = [];
    for (const file of walk(SRC)) {
      if (ALLOWED.some(a => file.endsWith(a))) continue;
      const lines = fs.readFileSync(file, 'utf8').split('\n');
      lines.forEach((line, i) => {
        if (!/toLocale(Date|Time)?String\s*\(/.test(line)) return;
        // Attribute values (aria-label / title) are not element children, so
        // they do not trip React's text-hydration check.
        if (/aria-label=|title:|title=/.test(line)) return;
        // Explicit, justified opt-out. Deliberately requires the marker to sit
        // in the preceding few lines WITH a reason, so an exception is visible
        // in review rather than hidden in a path allow-list.
        const preceding = lines.slice(Math.max(0, i - 4), i).join('\n');
        if (preceding.includes('local-format-ok:')) return;
        offenders.push(`${path.relative(SRC, file)}:${i + 1}: ${line.trim()}`);
      });
    }
    // jest's expect() takes no message argument, so the guidance rides in the
    // compared value — it has to reach whoever this fails on.
    const report = offenders.length
      ? [
          'Locale-dependent text rendered directly. The server (UTC) and the',
          "browser (the user's timezone/locale) produce different strings —",
          'React #418, the hydration error that hit /targets in prod. Use',
          'LocalDate / LocalDateTime / LocalNumber from @/components/LocalFormat.',
          '',
          ...offenders,
        ].join('\n')
      : '';
    expect(report).toBe('');
  });
});
