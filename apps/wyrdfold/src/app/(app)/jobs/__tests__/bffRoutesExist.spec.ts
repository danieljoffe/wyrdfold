import { readdirSync, readFileSync, existsSync, statSync } from 'fs';
import { join } from 'path';

/**
 * Every `/api/...` path the jobs feature fetches must have a Next route file.
 *
 * This exists because `useJobRemove` shipped calling `/api/jobs/{id}/remove`
 * with no `app/api/jobs/[id]/remove/route.ts` behind it. Jest mocks `fetch`,
 * so every unit test passed while the real call would have 404'd at Next
 * before reaching the API. Typecheck can't see it either — the path is a
 * string.
 *
 * The BFF hop is also where a request body gets silently dropped when a proxy
 * forgets to forward it, so "does the file exist" is the cheap half of a check
 * worth having at all.
 */

const FEATURE_DIR = join(__dirname, '..');
const API_DIR = join(__dirname, '..', '..', '..', 'api');

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap(entry => {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      return entry === '__tests__' ? [] : walk(full);
    }
    return full.endsWith('.ts') || full.endsWith('.tsx') ? [full] : [];
  });
}

/**
 * Turn a fetched path into the route directory it requires:
 *   `/api/jobs/${id}/remove`        -> api/jobs/[id]/remove
 *   `/api/jobs/tailor/${r.id}/x`    -> api/jobs/tailor/[id]/x
 * Query strings and trailing slashes are stripped.
 */
function toRouteDir(apiPath: string): string {
  const withoutQuery = apiPath.split('?')[0] ?? apiPath;
  return withoutQuery
    .replace(/^\/api\//, '')
    .split('/')
    .filter(Boolean)
    .map(seg => (seg.includes('${') ? '[id]' : seg))
    .join('/');
}

/** Route dirs that legitimately use a differently-named dynamic segment. */
const DYNAMIC_ALIASES = ['[id]', '[provider]', '[jobId]', '[targetId]'];

function routeExists(routeDir: string): boolean {
  const candidates = [routeDir];
  // A `[id]` guess may really be `[provider]` etc. — accept any single
  // dynamic segment at that position.
  for (const alias of DYNAMIC_ALIASES) {
    candidates.push(routeDir.replace(/\[id\]/g, alias));
  }
  return candidates.some(c => existsSync(join(API_DIR, c, 'route.ts')));
}

describe('jobs feature BFF routes', () => {
  const files = walk(FEATURE_DIR);

  it('finds source files to scan (the scan itself must not be vacuous)', () => {
    expect(files.length).toBeGreaterThan(10);
  });

  it('has a Next route file behind every /api path it fetches', () => {
    const missing: string[] = [];
    const seen = new Set<string>();

    for (const file of files) {
      const src = readFileSync(file, 'utf8');
      // fetch('/api/...') and fetch(`/api/...`)
      const matches = src.matchAll(/fetch\(\s*[`'"](\/api\/[^`'"]+)[`'"]/g);
      for (const m of matches) {
        const apiPath = m[1];
        if (!apiPath) continue;
        const routeDir = toRouteDir(apiPath);
        if (seen.has(routeDir)) continue;
        seen.add(routeDir);
        if (!routeExists(routeDir)) {
          missing.push(`${apiPath}  ->  app/api/${routeDir}/route.ts`);
        }
      }
    }

    expect(seen.size).toBeGreaterThan(3); // the scan found real call sites
    expect(missing).toEqual([]);
  });
});
