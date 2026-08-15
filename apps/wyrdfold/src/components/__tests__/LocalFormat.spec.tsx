import fs from 'node:fs';
import path from 'node:path';
import { render } from '@testing-library/react';
import { renderToStaticMarkup } from 'react-dom/server';
import {
  LocalDate,
  LocalDateTime,
  LocalNumber,
  formatLocalDateTime,
} from '../LocalFormat';

/**
 * Regression cover for two prod defects in the same components.
 *
 * 2026-08-08 — `/targets` threw React #418 ("text content does not match
 * server-rendered HTML") on every load because `TargetCard` rendered
 * `new Date(updated_at).toLocaleDateString()`, which resolves against the
 * *host's* locale and timezone — UTC on the server, the user's own settings in
 * the browser. Measured on the live row: server `"8/8/2026"` vs a US-West
 * browser `"8/7/2026"`.
 *
 * 2026-08-14 — the `suppressHydrationWarning` fix for the above stopped React
 * *correcting* the mismatch as well as warning about it, so every date froze at
 * the server's UTC rendering. Measured live at 23:37 local (06:37Z the next
 * day), a card read **"8/15/2026"** — tomorrow.
 *
 * HOW THE SECOND ONE IS ASSERTED: `suppressHydrationWarning` is consumed by
 * React and never reaches the DOM, so it cannot be observed from either
 * `container` or the server markup — an assertion on it would certify nothing.
 * What matters is observable: WHICH locale and timezone each render pass asks
 * for. Spying on the `toLocale*` call and reading its arguments distinguishes
 * the pre-hydration pass (pinned, deterministic) from the post-mount one (the
 * viewer's own), and fails if either disappears.
 *
 * These assertions are deliberately timezone-agnostic — `process.env.TZ`
 * pinning in jest is honoured on macOS and ignored on Linux CI, so a test that
 * depends on the ambient zone passes locally and rots in CI.
 */

const VALUE = '2026-08-08T06:50:44Z';

describe('locale-dependent text survives hydration', () => {
  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('LocalDate pins locale and timezone on the server render', () => {
    const spy = jest.spyOn(Date.prototype, 'toLocaleDateString');
    renderToStaticMarkup(<LocalDate value={VALUE} />);
    expect(spy).toHaveBeenCalledWith(
      'en-US',
      expect.objectContaining({ timeZone: 'UTC' })
    );
  });

  it('LocalDate server markup is the deterministic UTC rendering', () => {
    // The exact string the browser used to be stuck with. No ambient-timezone
    // dependency: locale and zone are both pinned for this pass.
    expect(
      renderToStaticMarkup(<LocalDate value='2026-08-15T06:37:10Z' />)
    ).toContain('8/15/2026');
  });

  it("LocalDate re-formats in the viewer's own locale after mount", () => {
    const spy = jest.spyOn(Date.prototype, 'toLocaleDateString');
    render(<LocalDate value={VALUE} />);
    // First pass pinned so hydration matches...
    expect(spy.mock.calls[0]).toEqual(['en-US', { timeZone: 'UTC' }]);
    // ...then a real re-render that hands formatting back to the browser.
    expect(spy.mock.calls.length).toBeGreaterThan(1);
    expect(spy.mock.calls.at(-1)).toEqual([undefined, undefined]);
  });

  it("LocalDateTime re-formats in the viewer's own locale after mount", () => {
    const spy = jest.spyOn(Date.prototype, 'toLocaleString');
    render(<LocalDateTime value={VALUE} />);
    expect(spy.mock.calls[0]).toEqual(['en-US', { timeZone: 'UTC' }]);
    expect(spy.mock.calls.at(-1)).toEqual([undefined, undefined]);
  });

  it("LocalNumber re-formats in the viewer's own locale after mount", () => {
    const spy = jest.spyOn(Number.prototype, 'toLocaleString');
    render(<LocalNumber value={1234567} />);
    expect(spy.mock.calls[0]).toEqual(['en-US', undefined]);
    expect(spy.mock.calls.at(-1)).toEqual([undefined, undefined]);
  });

  it('a caller-pinned timezone is respected, not overridden, before mount', () => {
    const spy = jest.spyOn(Date.prototype, 'toLocaleDateString');
    renderToStaticMarkup(
      <LocalDate value={VALUE} options={{ timeZone: 'America/New_York' }} />
    );
    expect(spy).toHaveBeenCalledWith('en-US', {
      timeZone: 'America/New_York',
    });
  });
});

describe('LocalFormat components', () => {
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
