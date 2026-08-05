import type { MissingSkill, SkillFrequency } from './types';

/**
 * Display-side hygiene for LLM-derived skill rows (#605).
 *
 * The analysis scorecards store skill names verbatim from the model, so
 * the insights aggregation ships case variants as separate rows ("State
 * management" vs "State Management") and leaks grader evidence clauses
 * into user-facing copy ("Automated testing — listed in skills with no
 * evidence refs"). Until the API-side aggregation normalizes at write
 * time, fold at the display boundary:
 *
 *   - strip an em-dash evidence clause (" — …") off the label;
 *   - merge rows whose cleaned label differs only by case/whitespace,
 *     summing counts (weighted mean for avg score).
 */
export function cleanSkillLabel(raw: string): string {
  const emDash = raw.indexOf(' — ');
  const base = emDash === -1 ? raw : raw.slice(0, emDash);
  return base.replace(/\s+/g, ' ').trim();
}

function foldKey(label: string): string {
  return label.toLowerCase();
}

export function foldSkillFrequencies(rows: SkillFrequency[]): SkillFrequency[] {
  const merged = new Map<string, SkillFrequency>();
  for (const row of rows) {
    const label = cleanSkillLabel(row.skill);
    const key = foldKey(label);
    const prev = merged.get(key);
    if (!prev) {
      merged.set(key, { ...row, skill: label });
    } else {
      prev.matched_count += row.matched_count;
      prev.missing_count += row.missing_count;
    }
  }
  return [...merged.values()].sort(
    (a, b) =>
      b.matched_count + b.missing_count - (a.matched_count + a.missing_count)
  );
}

export function foldMissingSkills(rows: MissingSkill[]): MissingSkill[] {
  const merged = new Map<
    string,
    MissingSkill & { _scoreWeight: number; _scoreSum: number }
  >();
  for (const row of rows) {
    const label = cleanSkillLabel(row.skill);
    const key = foldKey(label);
    const prev = merged.get(key);
    const weight = row.avg_job_score !== null ? row.missing_count : 0;
    const sum = row.avg_job_score !== null ? row.avg_job_score * weight : 0;
    if (!prev) {
      merged.set(key, {
        ...row,
        skill: label,
        _scoreWeight: weight,
        _scoreSum: sum,
      });
    } else {
      prev.missing_count += row.missing_count;
      prev.priority_score = Math.max(prev.priority_score, row.priority_score);
      prev._scoreWeight += weight;
      prev._scoreSum += sum;
    }
  }
  return [...merged.values()]
    .map(({ _scoreWeight, _scoreSum, ...row }) => ({
      ...row,
      avg_job_score: _scoreWeight > 0 ? _scoreSum / _scoreWeight : null,
    }))
    .sort((a, b) => b.priority_score - a.priority_score);
}
