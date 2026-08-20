-- ---------------------------------------------------------------------------
-- NULL out implausible provider posted dates.
--
-- ``services/date_normalize.py`` bounded the EPOCH parse path but not the ISO
-- one, so a handful of listings were written with dates like 1709-06-16,
-- 1766-01-09 and 1781-02-02 — parse artefacts, not data.
--
-- They are not cosmetic. ``source_posted_at`` is what the list shows as
-- "Posted", what the Posted sort orders by, and what the recency decay ages the
-- score against:
--   * a listing dated 1709 is permanently decayed to the score floor, and
--   * with the Posted sort now ordering the provider's date, ASCENDING page 1
--     opened on year 1709 (verified against prod 2026-08-17).
--
-- NULL is the correct value: it means "the ATS gave us no usable date", which
-- is exactly true. Those rows then render an em dash and sort last, alongside
-- the ~4% of listings that genuinely carry no provider date.
--
-- Bounds mirror ``date_normalize._plausible`` — keep the two in step. The floor
-- is deliberately loose (2000) so a genuinely long-open req survives; the
-- ceiling is tight because a listing cannot be posted in the future, with two
-- days of slack for clock skew at the source.
--
-- Idempotent, and safe to run before or after the code deploy: it only ever
-- turns garbage into NULL, which every reader already handles.
-- ---------------------------------------------------------------------------

UPDATE public.jobs
   SET source_posted_at = NULL
 WHERE source_posted_at IS NOT NULL
   AND (
        source_posted_at < TIMESTAMPTZ '2000-01-01'
     OR source_posted_at > now() + INTERVAL '2 days'
   );
