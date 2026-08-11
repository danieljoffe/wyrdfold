/**
 * Fetch a posting's JD text for a tailor kick-off.
 *
 * `POST /tailor/resume` requires the JD alongside `job_posting_id`, and list
 * payloads deliberately omit `description_html` to stay small — so the
 * detail GET is the only place to get it. Shared by `ResumeSection` and
 * `CoverLetterSection`, which had drifting copies of this.
 *
 * Returns the trimmed JD, or a reason the caller renders — no toasting here,
 * so the message can name the document type being generated.
 */
export type JobDescriptionResult =
  { ok: true; jd: string } | { ok: false; reason: 'fetch_failed' | 'empty' };

export async function loadJobDescription(
  jobPostingId: string
): Promise<JobDescriptionResult> {
  let detail: { description_html: string | null };
  try {
    const res = await fetch(`/api/jobs/${jobPostingId}`);
    if (!res.ok) return { ok: false, reason: 'fetch_failed' };
    detail = (await res.json()) as { description_html: string | null };
  } catch {
    return { ok: false, reason: 'fetch_failed' };
  }
  const jd = (detail.description_html ?? '').trim();
  return jd ? { ok: true, jd } : { ok: false, reason: 'empty' };
}
