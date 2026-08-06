/**
 * Canonical action inventory for the stress sweep. "100% coverage" means:
 * every id here either has a ledger row (executed) or an exclusion reason
 * (deliberate). The zz-coverage-gate spec fails on anything else — that is
 * the difference between measured coverage and aspirational coverage.
 *
 * Exclusion classes:
 *  - destructive: irreversibly mutates owner data (never in an automated sweep)
 *  - spend-heavy: LLM cost beyond the approved ~$0.30 cap
 *  - payment: real money surfaces (Stripe checkout)
 *  - operator: not reachable from the UI (cron/poll/internal endpoints)
 *  - gated: feature-flagged off in prod (signup/waitlist surfaces)
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
  { id: 'public.search.load', surface: 'search' },
  { id: 'search.query.frontend', surface: 'search' },
  { id: 'search.query.refine', surface: 'search' },
  { id: 'search.filter.recency', surface: 'search' },
  { id: 'search.filter.salary', surface: 'search' },
  { id: 'search.filter.location', surface: 'search' },
  { id: 'search.paginate.next', surface: 'search' },
  { id: 'search.listing.open-modal', surface: 'search' },
  { id: 'search.listing.full-page', surface: 'search' },
  { id: 'search.signup-mode.probe', surface: 'search' },

  // ---- Auth ---------------------------------------------------------------
  { id: 'auth.magic-link.consume', surface: 'auth' },

  // ---- Dashboard ----------------------------------------------------------
  { id: 'dash.today.load', surface: 'dashboard' },
  { id: 'dash.toggle.trends', surface: 'dashboard' },
  { id: 'dash.trends.charts-render', surface: 'dashboard' },
  { id: 'dash.trends.period.7d', surface: 'dashboard' },
  { id: 'dash.toggle.back-to-today', surface: 'dashboard' },
  { id: 'dash.top-match.click-through', surface: 'dashboard' },
  { id: 'insights.redirect', surface: 'dashboard' },

  // ---- Jobs list ----------------------------------------------------------
  { id: 'jobs.list.load', surface: 'jobs' },
  { id: 'jobs.tab.target', surface: 'jobs' },
  { id: 'jobs.tab.all', surface: 'jobs' },
  { id: 'jobs.sort.company', surface: 'jobs' },
  { id: 'jobs.sort.posted', surface: 'jobs' },
  { id: 'jobs.sort.score-restore', surface: 'jobs' },
  { id: 'jobs.filter.min-score', surface: 'jobs' },
  { id: 'jobs.filter.status', surface: 'jobs' },
  { id: 'jobs.filter.remote-only', surface: 'jobs' },
  { id: 'jobs.filter.min-salary', surface: 'jobs' },
  { id: 'jobs.filter.clear', surface: 'jobs' },
  { id: 'jobs.search.title', surface: 'jobs' },
  { id: 'jobs.load-more', surface: 'jobs' },
  { id: 'jobs.row.expand-panel', surface: 'jobs' },
  { id: 'jobs.panel.breakdown-render', surface: 'jobs' },
  { id: 'jobs.panel.status-history', surface: 'jobs' },
  { id: 'jobs.panel.status.to-saved', surface: 'jobs' },
  { id: 'jobs.panel.status.back-to-new', surface: 'jobs' },
  { id: 'jobs.panel.analysis.llm-run', surface: 'jobs' },
  { id: 'jobs.panel.close', surface: 'jobs' },
  {
    id: 'jobs.panel.feedback.vote',
    surface: 'jobs',
    excluded: 'destructive: writes a learning signal to the owner model',
  },
  {
    id: 'jobs.panel.delete-posting',
    surface: 'jobs',
    excluded: 'destructive: removes owner data',
  },
  {
    id: 'jobs.add-manual-url',
    surface: 'jobs',
    excluded: 'destructive: creates catalog rows with no UI undo',
  },

  // ---- Job detail page ----------------------------------------------------
  { id: 'jobdetail.page.load', surface: 'jobdetail' },
  { id: 'jobdetail.axes-breakdown.render', surface: 'jobdetail' },
  { id: 'jobdetail.description.toggle', surface: 'jobdetail' },

  // ---- Tailor: resume review ----------------------------------------------
  { id: 'resume.page.load', surface: 'tailor' },
  { id: 'resume.generate.llm', surface: 'tailor' },
  { id: 'resume.edit.autosave', surface: 'tailor' },
  { id: 'resume.versions.open', surface: 'tailor' },
  { id: 'resume.checkpoint', surface: 'tailor' },
  { id: 'resume.approve', surface: 'tailor' },
  { id: 'resume.unapprove', surface: 'tailor' },
  { id: 'resume.download.docx', surface: 'tailor' },
  {
    id: 'resume.readapt.llm',
    surface: 'tailor',
    excluded: 'destructive: replaces the owner-edited draft content',
  },

  // ---- Tailor: cover letter -----------------------------------------------
  { id: 'cover.page.load', surface: 'tailor' },
  { id: 'cover.generate.llm', surface: 'tailor' },
  { id: 'cover.download.docx', surface: 'tailor' },

  // ---- Targets ------------------------------------------------------------
  { id: 'targets.list.load', surface: 'targets' },
  { id: 'targets.activate', surface: 'targets' },
  { id: 'targets.detail.load', surface: 'targets' },
  { id: 'targets.detail.status-poll', surface: 'targets' },
  { id: 'targets.detail.preferences.load', surface: 'targets' },
  { id: 'targets.detail.reference-jds.load', surface: 'targets' },
  { id: 'targets.detail.learning-log.load', surface: 'targets' },
  { id: 'targets.deactivate-restore', surface: 'targets' },
  {
    id: 'targets.create.from-url',
    surface: 'targets',
    excluded: 'destructive: creates a shared target + reference JD',
  },
  {
    id: 'targets.create.from-label',
    surface: 'targets',
    excluded: 'destructive: creates a shared target',
  },
  {
    id: 'targets.learn.apply',
    surface: 'targets',
    excluded: 'destructive: mutates the shared scoring profile',
  },

  // ---- Profile ------------------------------------------------------------
  { id: 'profile.page.load', surface: 'profile' },
  { id: 'profile.identity.load', surface: 'profile' },
  { id: 'profile.experience.prose.load', surface: 'profile' },
  { id: 'profile.experience.optimized.load', surface: 'profile' },
  { id: 'profile.gap-health.load', surface: 'profile' },
  { id: 'profile.resume-style.load', surface: 'profile' },
  { id: 'profile.llm-usage.load', surface: 'profile' },
  {
    id: 'profile.experience.derive',
    surface: 'profile',
    excluded: 'destructive+spend: rebuilds the optimized profile doc',
  },
  {
    id: 'profile.upload-resume',
    surface: 'profile',
    excluded: 'destructive: replaces experience source material',
  },
  {
    id: 'profile.account.delete',
    surface: 'profile',
    excluded: 'destructive: account deletion',
  },

  // ---- Settings / billing -------------------------------------------------
  { id: 'settings.page.load', surface: 'settings' },
  { id: 'settings.notifications.load', surface: 'settings' },
  { id: 'settings.keys.load', surface: 'settings' },
  { id: 'settings.billing.account.load', surface: 'settings' },
  {
    id: 'settings.billing.checkout',
    surface: 'settings',
    excluded: 'payment: real Stripe checkout',
  },
  {
    id: 'settings.billing.portal',
    surface: 'settings',
    excluded: 'payment: opens the live Stripe portal session',
  },

  // ---- Onboarding ---------------------------------------------------------
  { id: 'onboarding.completed-redirect', surface: 'onboarding' },
  {
    id: 'onboarding.wizard.steps',
    surface: 'onboarding',
    excluded: 'destructive: requires resetting the owner profile to re-enter',
  },

  // ---- Operator / internal BFF routes (not UI-reachable) ------------------
  { id: 'bff.health', surface: 'bff' },
  {
    id: 'bff.jobs.poll',
    surface: 'bff',
    excluded: 'operator: cron-fired poll trigger',
  },
  {
    id: 'bff.email.target-paused',
    surface: 'bff',
    excluded: 'operator: internal email hook',
  },
  {
    id: 'bff.waitlist',
    surface: 'bff',
    excluded: 'gated: signup is invite-only in prod',
  },
];
