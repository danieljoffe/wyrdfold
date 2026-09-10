-- #959: persist WHY a job was excluded, not just that it was.
--
-- ``_title_matches_any_target`` already admits negative-keyword rejections
-- into the catalog on purpose — its comment says so plainly: "the scoring
-- pipeline records the rejection (excluded=True) for audit […] a user asking
-- 'why isn't this in my list?' gets an answer". We pay the ingestion cost of
-- that audit trail today but store only the verdict, never the reason.
--
-- Measured on prod before writing this (2026-09-05): of 204,078 rows flagged
-- ``excluded``, 98,406 carry a phase-1 ``promising = FALSE`` and 1,300 carry
-- ``logistics_filters``; 105,213 carry neither and so cannot be explained from
-- stored data at all. That gap is what this column closes.
--
-- Do NOT read that gap as "all negative-keyword exclusions" (an earlier version
-- of this comment did, and review of #1018 was right to reject it): ``excluded``
-- has two other writers: the Phase-2 empty-JD drop in
-- ``services/fit/score_persistence.py``, and the Phase-1 backfill in
-- ``services/relevance/phase1_backfill.py``, which ORs ``not promising`` into
-- the flag. Re-measured against the empty-JD signature (``scoring_status =
-- 'complete'`` + blank ``description_html``) it accounts for 2 rows, so the
-- number barely moves — but the inference was unsound regardless of how small
-- that population turned out to be. A test pins the writer set so a fourth
-- cannot appear without someone deciding whether it records a reason.
--
-- Why now, rather than with the surface that reads it (#959): the reason is
-- NOT reconstructible after the fact. A target's negative-keyword list moves
-- with its ``profile_version``, so a row scored under an older profile can no
-- longer be explained by replaying today's keywords. Every day this goes
-- unrecorded is permanently unexplainable. (Contrast ``matched_keywords``,
-- dropped in R2 as write-only — that one WAS reconstructible from the
-- breakdown JSONB, which is exactly why dropping it was safe.)
--
-- NULLABLE ON PURPOSE. Three states must stay distinguishable:
--   NULL  -> NO keyword finding was recorded for this row. Do NOT infer
--            whether a keyword fired. This is NOT only a legacy state: the
--            Phase-1 backfill UPSERTS scores rows carrying only
--            (promising, phase1_confidence, excluded), so on insert it CREATES
--            a NULL-keyword row long after this migration. "Predates the
--            column" was too narrow (review of #1018).
--   '{}'  -> this scoring pass recorded that NO title negative keyword fired.
--            That is all it says. It is NOT evidence of any other cause: the
--            row may be excluded by the phase-1 prefilter, by the Phase-2
--            empty-JD drop, or not be excluded at all.
--   {...} -> these negative keywords matched the TITLE in this pass.
-- A NOT NULL DEFAULT '{}' would collapse the first two and make every legacy
-- row assert "no keyword fired", which is a claim we cannot make.
--
-- The column describes ONE cause. Anything explaining a skip to a user must
-- combine it with ``excluded`` / ``promising`` / ``logistics_filters`` rather
-- than treating '{}' as proof of a particular alternative.
--
-- No backfill: see above, it is not derivable. Old rows stay NULL.
-- Additive and nullable, so this is a metadata-only DDL — no table rewrite.
-- Applied to production 2026-09-10 against 503,761 rows; the figure here was
-- written as "~482k" from a count taken days earlier, and a stale magnitude in
-- a migration note is the kind of thing a later reader sizes a maintenance
-- window from. COMMENT-ONLY correction — the DDL above is unchanged and
-- already applied.
--
-- ROLLOUT ORDER — MIGRATION FIRST, and note which direction is the safe one:
-- the OLD API tolerates this column being PRESENT (it never mentions it, and
-- PostgREST ignores unknown columns on read). The NEW API does NOT tolerate it
-- being ABSENT — _score_row_payload writes the key unconditionally, so every
-- scoring write fails with PGRST204 against a pre-migration schema. Apply this
-- before the API deploys. (An earlier version of this comment stated the
-- tolerance backwards, which would have justified exactly the wrong ordering
-- to someone reading only the SQL; caught in review of #1018.)

ALTER TABLE public.scores
  ADD COLUMN IF NOT EXISTS exclusion_keywords text[],
  -- PROVENANCE. Other writers advance ``scored_profile_version`` without
  -- touching ``exclusion_keywords`` (the Phase-2 paths in
  -- fit/score_persistence.py write ``scored_profile_version =
  -- target.profile_version``), so a row can read as current at v4 while
  -- carrying a keyword fact recorded at v3 — under which the keyword may not
  -- even have been a negative any more. Without this column a reader cannot
  -- tell, and "records what fired during THIS pass" would be false the moment
  -- another writer touched the row (caught in review of #1018).
  --
  -- Set to the ``scored_profile_version`` of the pass that wrote the array.
  --
  -- READER RULE — the keywords describe the row's CURRENT profile iff:
  --   exclusion_keywords            IS NOT NULL
  --   AND exclusion_keywords_version IS NOT NULL
  --   AND exclusion_keywords_version = scored_profile_version
  --
  -- RECORDEDNESS IS PART OF CURRENTNESS. An earlier draft wrote this as
  -- ``exclusion_keywords_version IS NOT DISTINCT FROM scored_profile_version``,
  -- which is wrong: that operator treats NULL/NULL as EQUAL, so every legacy
  -- row — where both are NULL precisely because nothing was ever recorded —
  -- would satisfy it and read as "current". Version equality alone cannot
  -- distinguish "the fact is current" from "we have no fact". Caught in review
  -- of #1018.
  --
  -- When the rule is false the keywords are a historical fact from an older
  -- profile: still worth keeping (it is why the row was excluded, and it is not
  -- reconstructible), but it must not be presented as the current reason.
  ADD COLUMN IF NOT EXISTS exclusion_keywords_version integer;

COMMENT ON COLUMN public.scores.exclusion_keywords_version IS
  'The scored_profile_version of the pass that wrote exclusion_keywords. '
  'Other writers advance scored_profile_version without touching the array. '
  'CURRENT iff exclusion_keywords IS NOT NULL AND exclusion_keywords_version '
  'IS NOT NULL AND exclusion_keywords_version = scored_profile_version. Do NOT '
  'write this as IS NOT DISTINCT FROM: that treats NULL/NULL as equal, so a '
  'legacy never-recorded row would read as current. Otherwise the keywords are '
  'a historical fact from an older profile - keep them, but do not present '
  'them as the current reason.';

COMMENT ON COLUMN public.scores.exclusion_keywords IS
  'Negative keywords that matched the job TITLE and forced excluded=TRUE. '
  'Records ONE cause, not the whole exclusion state machine — excluded has '
  'other writers (the Phase-2 empty-JD drop in fit/score_persistence.py and '
  'the Phase-1 backfill in relevance/phase1_backfill.py; neither writes this '
  'column, so a recorded finding survives them). '
  'NULL = no keyword finding recorded for this row (legacy, OR a non-scoring '
  'writer such as the Phase-1 backfill created it) - do not infer whether a '
  'keyword fired; {} = this pass '
  'recorded that no title keyword fired, which implies nothing about other '
  'causes; non-empty = these keywords matched the title. Set in '
  'app/services/scoring.py, persisted via _score_row_payload.';
