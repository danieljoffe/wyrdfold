import { useCallback, useEffect, useState } from 'react';

import type { TargetMembershipResponse, TargetRef } from './types';
import { type TargetsSource, useTargetsSource } from './useTargetsSource';

/**
 * Per-listing bind state for the URL-addressable detail routes (#467 §11.2
 * fast-follow) — the intercepted modal and the hard-load page each host the
 * detail OUTSIDE the explorer, so they carry their own membership read:
 *
 *  - `inTargets` — which of the caller's targets already contain this listing
 *    (the bound/unbound split → which actions render). Fetched from the same
 *    authed `/api/jobs/target-membership` the explorer uses, scoped to ONE id.
 *    Strictly best-effort, exactly like the explorer's page-level map: any
 *    failure leaves the listing "unbound" and NEVER blocks the detail.
 *    Membership is a per-user concept — never fetched logged-out.
 *  - `targetsSource` — the fetch-once targets list for the "Add to target"
 *    picker.
 *  - `handleAdded` — the live-unlock update (§11.3): merges a successful add
 *    into `inTargets` (deduped) so the detail flips to bound — match/tailor
 *    unlock — without a refetch.
 */
export function useListingBindings(
  jobId: string,
  isAuthenticated: boolean
): {
  inTargets: TargetRef[];
  targetsSource: TargetsSource;
  handleAdded: (target: TargetRef) => void;
} {
  const targetsSource = useTargetsSource();
  const [inTargets, setInTargets] = useState<TargetRef[]>([]);

  useEffect(() => {
    if (!isAuthenticated) return;
    let cancelled = false;
    void (async () => {
      try {
        const res = await fetch('/api/jobs/target-membership', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ job_posting_ids: [jobId] }),
        });
        if (!res.ok || cancelled) return;
        const data = (await res.json()) as TargetMembershipResponse;
        if (!cancelled && data.memberships) {
          setInTargets(data.memberships[jobId] ?? []);
        }
      } catch {
        // best-effort — the detail simply renders unbound
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [jobId, isAuthenticated]);

  const handleAdded = useCallback((t: TargetRef) => {
    setInTargets(prev =>
      // Dedup — a second add of the same target is a no-op for the state.
      prev.some(x => x.target_id === t.target_id) ? prev : [...prev, t]
    );
  }, []);

  return { inTargets, targetsSource, handleAdded };
}
