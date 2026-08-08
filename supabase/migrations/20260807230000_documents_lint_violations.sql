-- #656: persist ATS-lint state on tailored documents.
--
-- Until now a resume that failed ATS lint was generated, then THROWN AWAY —
-- POST /tailor/resume raised 422 and the ~39s of LLM work (and its cost
-- against the daily cap) was lost. Once generation moves to a background
-- runner the user may not even be watching when that happens, so the draft
-- is now persisted FLAGGED with its violations and can be edited and
-- re-checked without paying to regenerate.
--
-- Semantics of the column:
--   NULL  — never linted (every row predating this migration)
--   []    — linted clean
--   [...] — linted with violations; the document is a flagged draft
ALTER TABLE public.documents ADD COLUMN IF NOT EXISTS lint_violations jsonb;

COMMENT ON COLUMN public.documents.lint_violations IS
  'ATS lint state: NULL = never linted, [] = clean, [...] = flagged draft (#656)';
