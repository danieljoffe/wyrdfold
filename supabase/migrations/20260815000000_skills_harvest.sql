-- Skills harvest (plan-phase2-structured-harvest.md): persist the structured
-- skill facts the Phase-2 grader now emits from the same read that produces
-- the grade.
--
--   jobs.skills_required   — canonical, target-independent fact about the JD
--                            (≤8 normalized names; last-grader-writes-wins).
--   scores.skills_required — denormalized copy on the pair row, plus the
--   scores.skills_matched    pair-level facts, so the insights aggregation
--   scores.skills_missing    reads ONE table scoped by target_id instead of
--                            fanning out to jobs (#60-perf lesson).
--
-- All jsonb arrays of pre-normalized strings (lowercase, whitespace-collapsed,
-- evidence clauses stripped at write time — the #605 foldSkills debt closes at
-- the source). NULL = grade predates the harvest or the flag was off; the
-- write path omits the keys when the grader didn't emit them, so history is
-- never blanked by a flag flip.
--
-- additive: new nullable columns only. Safe to apply before the code that
-- writes them deploys; MUST be applied before it deploys (an UPDATE naming a
-- missing column fails the grade write). No backfill — columns populate as
-- jobs (re)grade.

ALTER TABLE public.jobs ADD COLUMN skills_required jsonb;
ALTER TABLE public.scores ADD COLUMN skills_required jsonb;
ALTER TABLE public.scores ADD COLUMN skills_matched jsonb;
ALTER TABLE public.scores ADD COLUMN skills_missing jsonb;
