import { headers } from 'next/headers';

import { bffSecretHeader } from '@/lib/api/bffSecret';
import { clientIpFromHeaders } from '@/lib/api/clientIp';

import type { JobSearchResult } from '../types';

/**
 * Server-side fetch of one public listing for the hard-load `/search/[id]`
 * page (#467 §11.2 fast-follow). Calls wyrdfold-api's `GET
 * /public/listings/{id}` directly with the BFF secret — the same trusted
 * posture as the `/api/public/*` forwarders without bouncing through our own
 * route handler (precedent: the landing page's server-side `signupMode()`
 * fetch reads `WYRDFOLD_API_URL` directly). Forwards the Vercel-trusted client
 * IP (`x-real-ip` via `next/headers`) so the API's per-IP limit keys on the
 * real visitor instead of pooling every hard load onto one egress IP.
 *
 * Returns the listing, or `null` on 404/422 — a missing/delisted id and a
 * junk-shaped id are equally "no such listing" to the page, which maps null to
 * `notFound()`. Any OTHER failure throws: an API outage must surface as an
 * error, not a misleading 404. Uncached (`no-store`): hard loads are the rare
 * path and the API already TTL-caches the row; the metadata + page reads of
 * one render collapse via React's fetch memoization.
 */
export async function fetchListing(
  id: string
): Promise<JobSearchResult | null> {
  const baseUrl = process.env['WYRDFOLD_API_URL'];
  if (!baseUrl) {
    throw new Error('WYRDFOLD_API_URL not configured');
  }
  const ip = clientIpFromHeaders(await headers());
  const res = await fetch(
    `${baseUrl}/public/listings/${encodeURIComponent(id)}`,
    {
      headers: {
        // NO Authorization — public data; membership is a separate authed call.
        ...bffSecretHeader(),
        ...(ip ? { 'x-forwarded-for': ip } : {}),
      },
      cache: 'no-store',
    }
  );
  if (res.status === 404 || res.status === 422) return null;
  if (!res.ok) {
    throw new Error(`Listing fetch failed (${res.status})`);
  }
  return (await res.json()) as JobSearchResult;
}
