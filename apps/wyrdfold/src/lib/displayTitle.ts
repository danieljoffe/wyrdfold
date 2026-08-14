/**
 * The display form of a posting title (ux-sweep §B1 follow-through).
 *
 * `title_display` is the server-cleaned form (`app/services/titles.py`) and
 * is null/absent when the raw title needed no repair — or when the row came
 * from a path that doesn't serve the column yet (the RPC-backed jobs lists,
 * stage 2). Falling back to `title` is therefore always correct, never a
 * degradation. Stored titles are never rewritten (they feed dedupe keys);
 * this is the ONE place display picks the cleaned form.
 */
export function displayTitle(job: {
  title: string;
  title_display?: string | null;
}): string {
  return job.title_display ?? job.title;
}
