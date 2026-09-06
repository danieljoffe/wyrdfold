-- #959: persist WHY a job was excluded, not just that it was.
--
-- ``_title_matches_any_target`` already admits negative-keyword rejections
-- into the catalog on purpose — its comment says so plainly: "the scoring
-- pipeline records the rejection (excluded=True) for audit […] a user asking
-- 'why isn't this in my list?' gets an answer". We pay the ingestion cost of
-- that audit trail today but store only the verdict, never the reason.
--
-- Measured on prod before writing this (2026-09-05): of 204,078 rows flagged
-- ``excluded``, 105,213 (51.6%) carry no recoverable reason — no phase-1
-- ``promising = FALSE``, no ``logistics_filters``. Those are the
-- negative-keyword exclusions this column captures.
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
--   NULL  -> scored before this column existed; we do not know the reason,
--            and the UI must not invent one.
--   '{}'  -> recorded, and no negative keyword fired (excluded by the
--            phase-1 prefilter instead — see ``scores.promising``).
--   {...} -> recorded, and these keywords matched the TITLE.
-- A NOT NULL DEFAULT '{}' would collapse the first two and make every legacy
-- row read as "excluded, but nothing fired", which is a claim we cannot make.
--
-- No backfill: see above, it is not derivable. Old rows stay NULL.
-- Additive and nullable, so this is a metadata-only DDL — no table rewrite on
-- the ~482k-row table, and the deployed API tolerates the column's absence
-- (it only ever writes it), so the migration is safe to apply before merge.

ALTER TABLE public.scores
  ADD COLUMN IF NOT EXISTS exclusion_keywords text[];

COMMENT ON COLUMN public.scores.exclusion_keywords IS
  'Negative keywords that matched the job TITLE and forced excluded=TRUE. '
  'NULL = scored before the column existed (reason unknown, do not display); '
  '{} = recorded, no keyword fired (excluded by the phase-1 prefilter, see '
  'scores.promising); non-empty = the keywords that fired. Set in '
  'app/services/scoring.py, persisted via _score_row_payload.';
