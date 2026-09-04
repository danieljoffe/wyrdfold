-- #470: company-domain enrichment — the key logo services link by.
--
-- A "company" has no entity of its own: it's a free-text name copied from
-- sources onto every job row. Logo services key on a company DOMAIN, which
-- is stored nowhere and not derivable from the ATS board URL. sources is
-- ~one row per company board (unique board_token) and the poller already
-- loads it, so it's the natural once-per-company home (the research doc
-- docs/research-wyrdfold-company-logos.md carries the full design; the
-- owner's constraint: store only LINKS, never image copies — the client
-- builds provider URLs from this domain, so no logo_url column either;
-- switching providers is a client-side change).
--
-- Nullable and additive: rows without a verified domain render the
-- existing initials monogram, exactly as today.

ALTER TABLE public.sources ADD COLUMN IF NOT EXISTS domain text;

COMMENT ON COLUMN public.sources.domain IS
    '#470: company web domain (e.g. "datadoghq.com") — enrichment guesses '
    'candidates from company_name/board_token and stores one that answers '
    'HTTP. That check is WEAK: a wrong-but-live domain can be stored, and '
    'clients BUILD logo links from this value, so a bad row renders another '
    'company''s logo. Correct by setting it NULL. No image is ever stored.';
