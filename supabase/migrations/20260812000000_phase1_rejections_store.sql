-- Persist Phase-1 negative verdicts (docs/plan-phase1-rejection-persistence.md).
--
-- A Phase-1-rejected title never ingests, so it re-enters triage on every poll
-- of its source. The #514 negative cache remembered rejections in an
-- in-process dict with a 24h TTL — two redundant leaks: the TTL re-bills the
-- entire standing rejected corpus daily BY DESIGN, and near-daily Railway
-- deploys wipe the dict anyway. Measured 2026-08-12: 416,689 verdicts/7d of
-- which ~75-90% were re-judgments of already-rejected titles (63% of all LLM
-- spend). This table is the durable replacement.
--
-- Key semantics are IDENTICAL to the dict it replaces:
--   (target_id, profile_version, title_norm)
-- with title_norm = lowercase, whitespace-collapsed title. A profile edit
-- bumps profile_version, so every cached rejection under the old version
-- misses and the target re-judges everything under the new profile. Stale
-- profile_version rows die by retention sweep, not by trigger.
--
-- Only REJECTIONS are stored: admits ingest, and the known-external-id check
-- already stops their re-triage (#514 semantics, unchanged).
--
-- `confidence` and `model` are observability-only — NOT part of the key. A
-- prompt/model change does not auto-invalidate; after a material change run
-- `delete from phase1_rejections;` (the corpus re-warms in ~a day of polls).
--
-- additive: new table only, no existing objects touched. Safe to apply before
-- the code that reads it deploys (the reader fail-opens to the LLM on any
-- store error, including "table does not exist").

CREATE TABLE public.phase1_rejections (
    target_id       uuid NOT NULL REFERENCES public.targets(id) ON DELETE CASCADE,
    profile_version integer NOT NULL,
    title_norm      text NOT NULL,
    confidence      integer,
    model           text,
    judged_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (target_id, profile_version, title_norm)
);

-- Retention sweep path: delete ... where judged_at < cutoff. The PK covers the
-- read path (exact key membership); this covers the sweep without a seq scan.
CREATE INDEX idx_phase1_rejections_judged_at
    ON public.phase1_rejections (judged_at);

-- Service-role only, like llm_costs: RLS enabled with no policies. The poller
-- reads and writes through the service client; no user-facing surface touches
-- this table.
ALTER TABLE public.phase1_rejections ENABLE ROW LEVEL SECURITY;
