-- Lazy fit-score refresh (E2). The per-user cached ``user_targets.fit_score`` is
-- computed by an LLM against the user's experience profile at link time. When the
-- user later edits their profile, that cached score goes stale — the E fix
-- (resolve_current_payload) only freshened NEW targets; existing ones kept their
-- old score with no trigger to recompute.
--
-- This marks WHICH profile version each cached score was computed against — the
-- master ``experience_prose_docs`` id that resolve_current_payload scored against
-- (the same "current profile" definition). On view, a score whose marker != the
-- user's current prose doc is stale and gets refreshed lazily in the background.
--
-- NULL means either "computed before we tracked versions" or "not yet scored".
-- The reader distinguishes them by ``fit_score IS NOT NULL``: a scored row with a
-- NULL marker is treated as stale (refreshed once, then stamped current); an
-- unscored row is skipped. No FK to experience_prose_docs — a dangling id just
-- reads as "stale" and self-heals via a refresh, so referential integrity buys
-- nothing here.
alter table public.user_targets
    add column if not exists fit_score_prose_doc_id uuid;

comment on column public.user_targets.fit_score_prose_doc_id is
    'E2: the experience_prose_docs.id the cached fit_score was computed against. '
    'Stale when != the user''s current prose doc → lazy background refresh on view. '
    'NULL = pre-tracking (stale if fit_score set) or not yet scored.';
