-- job_embeddings slim-down, stage 2 — the 512-d lazy-embedding space.
--
-- RELEASE-COUPLED (NOT backward compatible — do NOT apply ahead of the
-- release like stage 1 was): ships with the code that (a) embeds LAZILY at
-- grade time (``ensure_job_vectors`` in the Phase-2 runner; embed-on-ingest
-- and the blanket backfill drain are removed) and (b) produces 512-d
-- Matryoshka vectors (``job_embeddings.EMBED_DIMENSIONS``, voyage-3.5
-- ``output_dimension``). Apply at the release gate, migrations-first: during
-- the brief window where OLD code still runs, its 1024-d embed writes fail
-- the dimension check and are swallowed (every embed path is fail-soft by
-- contract), then the new code rebuilds on demand.
--
-- The vectors are a regenerable cache, and 1024-d vs 512-d are DIFFERENT
-- spaces (truncating a stored 1024-d vector is not the Matryoshka
-- projection — the model must emit 512-d natively), so wiping beats
-- casting. The lazy path re-materializes exactly the read set (~Phase-2
-- candidates, tens/day) per the "only a few will ever be read" directive —
-- most of the 15.6k stage-1 vectors would never have been read again.

-- guarded-destructive: job_embeddings is a regenerable cache (vectors are
-- re-derivable from jobs.title/description_html at ~$0.00005/job via the
-- lazy ensure path); no snapshot needed — the source of truth is untouched.
TRUNCATE public.job_embeddings;

ALTER TABLE public.job_embeddings
  ALTER COLUMN embedding TYPE extensions.halfvec(512)
  USING NULL::extensions.halfvec(512);

-- Target vectors move to the same 512-d space: null them and their text
-- hash so the existing hash-guarded refresh re-embeds each target at next
-- use (11 rows, ~cents), and void the 1024-space cosine calibration — the
-- per-target thresholds are meaningless in the new space and must be
-- re-derived if/when the #90 gate is enforced (allowlist is empty today,
-- so nothing is gating on them).

ALTER TABLE public.targets
  ALTER COLUMN embedding TYPE extensions.halfvec(512)
  USING NULL::extensions.halfvec(512);

UPDATE public.targets
  SET embedding_text_hash = NULL,
      prescan_cosine_threshold = NULL;
