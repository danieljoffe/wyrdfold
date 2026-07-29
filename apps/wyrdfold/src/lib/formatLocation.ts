/**
 * Canonical location display (#518) — composes the structured parts the API
 * parses at ingest (`jobs.city/state/country/location_remote`) into the
 * "City, ST, Country" convention:
 *
 *   San Francisco, CA, US        London, UK          US
 *   Remote (US)                  Remote — Austin, TX, US
 *
 * Falls back to the raw `location` string when nothing was parsed
 * ("2 Locations", "Hybrid", campus names) — a parser miss can never render
 * worse than today. Returns '' when there is no location signal at all;
 * callers keep their own placeholder ('—').
 */

export interface LocationFields {
  location?: string | null;
  city?: string | null;
  state?: string | null;
  country?: string | null;
  location_remote?: boolean | null;
}

export function formatLocation(job: LocationFields): string {
  const parts = [job.city, job.state, job.country].filter(
    (p): p is string => typeof p === 'string' && p.length > 0
  );
  const remote = job.location_remote === true;

  if (parts.length === 0) {
    if (remote) return 'Remote';
    return (job.location ?? '').trim();
  }

  const place = parts.join(', ');
  if (!remote) return place;
  // Remote with ONLY a country reads best as a qualifier; with a real
  // city/state the place is a hub worth showing in full.
  if (!job.city && !job.state && job.country) return `Remote (${job.country})`;
  return `Remote — ${place}`;
}
