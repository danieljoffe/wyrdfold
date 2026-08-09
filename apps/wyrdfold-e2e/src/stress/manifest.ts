/**
 * Canonical action inventory for the stress sweep. "100% coverage" means:
 * every id here either has a ledger row (executed) or an exclusion reason
 * (deliberate). The zz-coverage-gate spec fails on anything else — that is
 * the difference between measured coverage and aspirational coverage.
 *
 * 2026-08-08: the owner authorised a full-blast sweep ("everything, no
 * exceptions") for the UX/UI coverage drive, so the destructive and
 * spend-heavy exclusions that guarded earlier runs are gone — those actions
 * are now implemented in ``stress-authed-deep.spec.ts`` with explicit
 * restore steps (throwaway target instead of a real one, approve→unapprove,
 * status flip→revert, onboarding reset→complete).
 *
 * Exclusion classes that REMAIN, and why:
 *  - unrecoverable: no restore path exists and the loss is real owner data
 *    (account deletion; résumé source replacement). Both are exercised up to
 *    their confirmation gate instead — see the ``*-dialog`` / ``*-validate``
 *    ids, which ARE executed.
 *  - payment-submit: creating a Stripe session is fine and IS covered; only
 *    the act of submitting payment is off-limits.
 */

export interface ManifestEntry {
  id: string;
  surface: string;
  excluded?: string;
}

export const MANIFEST: ManifestEntry[] = [
  // ---- Public surface -----------------------------------------------------
  { id: 'public.home.load', surface: 'public' },
  { id: 'public.terms.load', surface: 'public' },
  { id: 'public.privacy.load', surface: 'public' },
  { id: 'public.login.load', surface: 'public' },
  { id: 'public.404', surface: 'public' },
  { id: 'public.search.load', surface: 'search' },
  { id: 'search.query.frontend', surface: 'search' },
  { id: 'search.query.refine', surface: 'search' },
  { id: 'search.filter.recency', surface: 'search' },
  { id: 'search.filter.salary', surface: 'search' },
  { id: 'search.filter.location', surface: 'search' },
  { id: 'search.paginate.next', surface: 'search' },
  { id: 'search.listing.open-modal', surface: 'search' },
  { id: 'search.listing.full-page', surface: 'search' },
  { id: 'search.share-url.restore', surface: 'search' },
  { id: 'search.signup-mode.probe', surface: 'search' },

  // ---- Auth ---------------------------------------------------------------
  {
    id: 'auth.magic-link.consume',
    surface: 'auth',
    excluded:
      'minted out-of-band: src/stress/auth-setup.mjs consumes a real prod ' +
      'magic link and ledgers this id. A sweep that reuses the saved storage ' +
      'state legitimately does not perform the sign-in.',
  },

  // ---- Dashboard ----------------------------------------------------------
  { id: 'dash.today.load', surface: 'dashboard' },
  { id: 'dash.toggle.trends', surface: 'dashboard' },
  { id: 'dash.trends.charts-render', surface: 'dashboard' },
  { id: 'dash.trends.period.7d', surface: 'dashboard' },
  { id: 'dash.trends.period.90d', surface: 'dashboard' },
  { id: 'dash.trends.period.all', surface: 'dashboard' },
  { id: 'dash.trends.target-comparison.labels', surface: 'dashboard' },
  { id: 'dash.funnel.chip.saved', surface: 'dashboard' },
  { id: 'dash.toggle.back-to-today', surface: 'dashboard' },
  { id: 'dash.top-match.click-through', surface: 'dashboard' },
  { id: 'dash.theme.cycle', surface: 'dashboard' },
  { id: 'insights.redirect', surface: 'dashboard' },
  { id: 'app.404', surface: 'dashboard' },

  // ---- Jobs list ----------------------------------------------------------
  { id: 'jobs.list.load', surface: 'jobs' },
  { id: 'jobs.tab.target', surface: 'jobs' },
  { id: 'jobs.tab.all', surface: 'jobs' },
  { id: 'jobs.sort.company', surface: 'jobs' },
  { id: 'jobs.sort.posted', surface: 'jobs' },
  { id: 'jobs.sort.title', surface: 'jobs' },
  { id: 'jobs.sort.score-restore', surface: 'jobs' },
  { id: 'jobs.sort.request-fanout', surface: 'jobs' },
  { id: 'jobs.filter.min-score', surface: 'jobs' },
  { id: 'jobs.filter.min-score.floor-holds', surface: 'jobs' },
  { id: 'jobs.filter.min-score.decay-direction', surface: 'jobs' },
  { id: 'jobs.filter.status', surface: 'jobs' },
  { id: 'jobs.filter.remote-only', surface: 'jobs' },
  { id: 'jobs.filter.min-salary', surface: 'jobs' },
  { id: 'jobs.filter.country', surface: 'jobs' },
  { id: 'jobs.filter.location-include', surface: 'jobs' },
  { id: 'jobs.filter.location-exclude', surface: 'jobs' },
  { id: 'jobs.filter.clear', surface: 'jobs' },
  { id: 'jobs.search.title', surface: 'jobs' },
  { id: 'jobs.search.debounce', surface: 'jobs' },
  { id: 'jobs.load-more', surface: 'jobs' },
  { id: 'jobs.row.expand-panel', surface: 'jobs' },
  { id: 'jobs.panel.breakdown-render', surface: 'jobs' },
  { id: 'jobs.panel.status-history', surface: 'jobs' },
  { id: 'jobs.panel.status.to-saved', surface: 'jobs' },
  { id: 'jobs.panel.status.back-to-new', surface: 'jobs' },
  { id: 'jobs.panel.analysis.llm-run', surface: 'jobs' },
  { id: 'jobs.panel.target-membership', surface: 'jobs' },
  { id: 'jobs.panel.add-to-target', surface: 'jobs' },
  { id: 'jobs.panel.feedback.vote', surface: 'jobs' },
  { id: 'jobs.panel.close', surface: 'jobs' },
  { id: 'jobs.select-all', surface: 'jobs' },
  { id: 'jobs.select-row.keyboard', surface: 'jobs' },
  { id: 'jobs.batch.export-zip', surface: 'jobs' },
  { id: 'jobs.batch.tailor', surface: 'jobs' },
  { id: 'jobs.add-manual-url', surface: 'jobs' },
  { id: 'jobs.delete-posting', surface: 'jobs' },

  // ---- Job detail page ----------------------------------------------------
  { id: 'jobdetail.page.load', surface: 'jobdetail' },
  { id: 'jobdetail.axes-breakdown.render', surface: 'jobdetail' },
  { id: 'jobdetail.description.toggle', surface: 'jobdetail' },
  { id: 'jobdetail.status.change', surface: 'jobdetail' },
  { id: 'jobdetail.404.ghost-id', surface: 'jobdetail' },

  // ---- Tailor: resume review ----------------------------------------------
  { id: 'resume.page.load', surface: 'tailor' },
  { id: 'resume.generate.llm', surface: 'tailor' },
  { id: 'resume.edit.autosave', surface: 'tailor' },
  { id: 'resume.versions.open', surface: 'tailor' },
  { id: 'resume.checkpoint', surface: 'tailor' },
  { id: 'resume.approve', surface: 'tailor' },
  { id: 'resume.unapprove', surface: 'tailor' },
  { id: 'resume.ats-recheck', surface: 'tailor' },
  { id: 'resume.readapt.llm', surface: 'tailor' },
  { id: 'resume.download.docx', surface: 'tailor' },
  { id: 'resume.flagged-draft.render', surface: 'tailor' },

  // ---- Tailor: cover letter -----------------------------------------------
  { id: 'cover.page.load', surface: 'tailor' },
  { id: 'cover.generate.llm', surface: 'tailor' },
  { id: 'cover.edit.autosave', surface: 'tailor' },
  { id: 'cover.versions.open', surface: 'tailor' },
  { id: 'cover.checkpoint', surface: 'tailor' },
  { id: 'cover.approve', surface: 'tailor' },
  { id: 'cover.unapprove', surface: 'tailor' },
  { id: 'cover.flagged-draft.render', surface: 'tailor' },
  { id: 'cover.download.docx', surface: 'tailor' },

  // ---- Targets ------------------------------------------------------------
  { id: 'targets.list.load', surface: 'targets' },
  { id: 'targets.activate', surface: 'targets' },
  { id: 'targets.detail.load', surface: 'targets' },
  { id: 'targets.detail.status-poll', surface: 'targets' },
  { id: 'targets.detail.preferences.load', surface: 'targets' },
  { id: 'targets.detail.preferences.save', surface: 'targets' },
  { id: 'targets.detail.reference-jds.load', surface: 'targets' },
  { id: 'targets.detail.reference-jds.vote', surface: 'targets' },
  { id: 'targets.detail.learning-log.load', surface: 'targets' },
  { id: 'targets.detail.axis-weights.adjust', surface: 'targets' },
  { id: 'targets.detail.axis-weights.undo', surface: 'targets' },
  { id: 'targets.detail.notification-thresholds', surface: 'targets' },
  { id: 'targets.suggest.list', surface: 'targets' },
  { id: 'targets.suggest.lateral', surface: 'targets' },
  { id: 'targets.suggest.from-query', surface: 'targets' },
  { id: 'targets.search', surface: 'targets' },
  { id: 'targets.create.from-label', surface: 'targets' },
  { id: 'targets.create.from-url', surface: 'targets' },
  { id: 'targets.learn.run', surface: 'targets' },
  { id: 'targets.learn.reject', surface: 'targets' },
  { id: 'targets.link', surface: 'targets' },
  { id: 'targets.delete', surface: 'targets' },
  { id: 'targets.deactivate-restore', surface: 'targets' },

  // ---- Profile ------------------------------------------------------------
  { id: 'profile.page.load', surface: 'profile' },
  { id: 'profile.identity.load', surface: 'profile' },
  { id: 'profile.identity.save', surface: 'profile' },
  { id: 'profile.experience.prose.load', surface: 'profile' },
  { id: 'profile.experience.optimized.load', surface: 'profile' },
  { id: 'profile.experience.conversation.probe', surface: 'profile' },
  { id: 'profile.experience.derive', surface: 'profile' },
  { id: 'profile.gap-health.load', surface: 'profile' },
  { id: 'profile.resume-style.load', surface: 'profile' },
  { id: 'profile.llm-usage.load', surface: 'profile' },
  { id: 'profile.export.download', surface: 'profile' },
  { id: 'profile.keys.load', surface: 'profile' },
  { id: 'profile.keys.add-remove', surface: 'profile' },
  { id: 'profile.upload-resume.validate', surface: 'profile' },
  { id: 'profile.account.delete-dialog', surface: 'profile' },
  {
    id: 'profile.upload-resume.submit',
    surface: 'profile',
    excluded:
      'unrecoverable: replaces the owner’s experience source material and no ' +
      'original file exists to restore it. Covered to the validation gate by ' +
      'profile.upload-resume.validate.',
  },
  {
    id: 'profile.account.delete',
    surface: 'profile',
    excluded:
      'unrecoverable: destroys the account under test. Covered to the ' +
      'confirmation gate by profile.account.delete-dialog.',
  },

  // ---- Settings / billing -------------------------------------------------
  { id: 'settings.page.load', surface: 'settings' },
  { id: 'settings.notifications.load', surface: 'settings' },
  { id: 'settings.notifications.save', surface: 'settings' },
  { id: 'settings.keys.load', surface: 'settings' },
  { id: 'settings.billing.account.load', surface: 'settings' },
  { id: 'settings.billing.checkout.session', surface: 'settings' },
  { id: 'settings.billing.portal.session', surface: 'settings' },
  {
    id: 'settings.billing.pay',
    surface: 'settings',
    excluded:
      'payment-submit: submitting card details / confirming a charge is never ' +
      'automated. Session creation IS covered above.',
  },

  // ---- Onboarding ---------------------------------------------------------
  { id: 'onboarding.completed-redirect', surface: 'onboarding' },
  { id: 'onboarding.reset', surface: 'onboarding' },
  { id: 'onboarding.wizard.steps', surface: 'onboarding' },
  { id: 'onboarding.complete-restore', surface: 'onboarding' },

  // ---- Operator / internal BFF routes -------------------------------------
  { id: 'bff.health', surface: 'bff' },
  { id: 'bff.jobs.poll', surface: 'bff' },
  { id: 'bff.email.target-paused', surface: 'bff' },
  { id: 'bff.waitlist', surface: 'bff' },
  { id: 'bff.search-events', surface: 'bff' },
];
