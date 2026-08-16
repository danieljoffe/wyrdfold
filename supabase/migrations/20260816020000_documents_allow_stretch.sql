-- #785: persist the stretch opt-in on the document it produced.
--
-- #780 gave cover-letter generation an ``allow_stretch`` flag: on a job whose
-- match analysis says "Skip", the user confirms a pre-spend warning and the
-- model writes an honest stretch letter instead of declining to apply on their
-- behalf. That confirmation is a property of THIS letter — but nothing stored
-- it, so "Re-generate with AI" on the review page had no way to know the user
-- had already opted in, and the regenerated letter could come back a refusal.
--
-- The verdict itself cannot be re-derived on that surface: ``JobAnalysis``
-- recommendations are per-(job, target) and the review route has no target in
-- scope, so a job matching several targets has several verdicts. Storing the
-- answer the user actually gave is both cheaper and more faithful than
-- re-asking a question that has more than one answer.
--
-- Semantics:
--   false — generated on the default path (also every row predating this
--           column, which is accurate: they were generated without the opt-in)
--   true  — the user explicitly confirmed "I know it's a reach, write it anyway"
--
-- Applies to cover letters only in practice; resumes have no stretch addendum
-- and always write false. The column lives on ``documents`` because that is
-- the one table both document types share.
--
-- Metadata-only on PG11+ (NOT NULL with a non-volatile DEFAULT does not
-- rewrite the table).
ALTER TABLE public.documents
  ADD COLUMN IF NOT EXISTS allow_stretch boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN public.documents.allow_stretch IS
  'The user''s explicit stretch opt-in for this document, so a re-generate reuses it (#785)';
