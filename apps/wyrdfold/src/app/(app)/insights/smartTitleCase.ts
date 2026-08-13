/**
 * Title-caser for near-miss chips (ux-sweep 2026-08-12 §B1).
 *
 * The rejection store keys on `title_norm` — lowercased, whitespace-collapsed
 * — and keeps no original-casing column, so display casing must be
 * reconstructed. CSS `capitalize` mangled acronyms ("It Internal Auditor",
 * "Ai", "Iii"); this uppercases known acronyms and roman numerals instead.
 * Company-specific acronyms we can't know (e.g. "(Aht)") stay merely
 * capitalized — the durable fix is a `title_display` column on
 * `phase1_rejections` (plan §Phase 4), which would make this obsolete.
 */

/** Tokens that are (near-)always acronyms in a job title. Deliberately
 *  conservative: a false positive here ("Make IT Work") is worse than a
 *  leftover "Aht". */
const ACRONYMS = new Set([
  'ai',
  'api',
  'ar',
  'asic',
  'aws',
  'b2b',
  'b2c',
  'cad',
  'cd',
  'ci',
  'cnc',
  'cpu',
  'crm',
  'css',
  'd2c',
  'erp',
  'etl',
  'fpga',
  'gcp',
  'gpu',
  'gtm',
  'hr',
  'html',
  'hvac',
  'iot',
  'it',
  'llm',
  'ml',
  'nlp',
  'php',
  'plc',
  'qa',
  'rf',
  'sap',
  'sdet',
  'sdk',
  'sql',
  'sre',
  'ui',
  'ux',
  'vr',
]);

/** Mixed-case brand/term spellings that neither capitalize nor uppercase.
 *  A Map, not an object literal — `SPECIAL['constructor']` on an object
 *  would surface Object.prototype members for prototype-named words. */
const SPECIAL = new Map<string, string>([
  ['devops', 'DevOps'],
  ['iaas', 'IaaS'],
  ['ios', 'iOS'],
  ['javascript', 'JavaScript'],
  ['github', 'GitHub'],
  ['macos', 'macOS'],
  ['mysql', 'MySQL'],
  ['nosql', 'NoSQL'],
  ['paas', 'PaaS'],
  ['postgresql', 'PostgreSQL'],
  ['saas', 'SaaS'],
  ['typescript', 'TypeScript'],
]);

/** ii/iii/iv/vi…ix as level suffixes ("Engineer III"). Single i/v/x are
 *  handled fine by plain capitalization. */
const ROMAN = /^(?:i{2,3}|iv|vi{1,3}|ix)$/;

function transformWord(run: string): string {
  const lower = run.toLowerCase();
  const special = SPECIAL.get(lower);
  if (special) return special;
  if (ACRONYMS.has(lower) || ROMAN.test(lower)) return lower.toUpperCase();
  return lower.charAt(0).toUpperCase() + lower.slice(1);
}

export function smartTitleCase(title: string): string {
  return (
    title
      // Normalizer artifacts: "_field" → "field".
      .replace(/_+/g, ' ')
      .replace(/\s+/g, ' ')
      .trim()
      // A word-run keeps +, # and apostrophes ("c++", "c#", "master's");
      // &, / and - split runs so "cd&ai" and "c/c++" case per part.
      .replace(/[a-z0-9][a-z0-9+#'’]*/gi, transformWord)
  );
}
