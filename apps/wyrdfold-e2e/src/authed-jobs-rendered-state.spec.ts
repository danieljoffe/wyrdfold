import { createClient } from '@supabase/supabase-js';
import { expect, test } from './fixtures';

/**
 * Rendered-state journeys the 2026-08-05 prod drive proved the suite was
 * blind to (#608, #610). Every prior spec asserted *contracts* (URLs,
 * redirects, element presence) while prod shipped:
 *
 *   - score cells rendering an infinite "scoring" spinner for fully-graded
 *     rows (#603 — the API stamps ``scoring_status: 'stage2'`` on graded
 *     rows; nothing ever looked at a rendered cell);
 *   - the open analysis panel unmounting mid-read when the verdict
 *     re-ranked its row (#602 — nothing followed an analysis past the
 *     202);
 *   - a failed jobs fetch rendering as "No jobs found" (#604 — nothing
 *     distinguished failure from emptiness);
 *   - /onboarding re-offering the wizard to completed profiles (#607).
 *
 * This spec owns its data (sentinel ids, parallel-safe like the other
 * authed specs) and seeds REAL postings + scores rows — including the
 * exact ``stage2`` status value whose absence from fixtures hid #603.
 */

// Fixed sentinels so reruns upsert the same rows instead of multiplying.
const TARGET_ID = '00000000-0000-4000-8000-0000000f1177';
const SOURCE_ID = '00000000-0000-4000-8000-0000000f1180';
const JOB_GRADED_ID = '00000000-0000-4000-8000-0000000f1178';
const JOB_PENDING_ID = '00000000-0000-4000-8000-0000000f1179';
const OPTIMIZED_DOC_ID = '00000000-0000-4000-8000-0000000f1181';

test.beforeAll(async () => {
  const url = process.env['NEXT_PUBLIC_SUPABASE_URL'];
  const serviceKey = process.env['SUPABASE_SERVICE_ROLE_KEY'];
  const email = process.env['E2E_TEST_USER_EMAIL'];
  test.skip(!url || !serviceKey || !email, 'auth env not configured');

  const admin = createClient(url!, serviceKey!, {
    auth: { persistSession: false, autoRefreshToken: false },
  });

  const { data: userList, error: listErr } = await admin.auth.admin.listUsers();
  if (listErr) throw new Error(`listUsers failed: ${listErr.message}`);
  const user = userList.users.find(u => u.email === email);
  if (!user) throw new Error(`e2e user ${email} not found — run seed-e2e-user`);

  // Completed onboarding gates both the /onboarding redirect under test and
  // the dashboard chrome the other journeys pass through.
  const { error: profErr } = await admin.from('user_profiles').upsert(
    {
      user_id: user.id,
      email,
      onboarding_completed_at: new Date().toISOString(),
    },
    { onConflict: 'user_id' }
  );
  if (profErr)
    throw new Error(`user_profiles upsert failed: ${profErr.message}`);

  // The analysis flow refuses to run without an optimized experience doc
  // (the ``no_profile`` gate, #105) — the first draft of this spec passed
  // on that gate branch without ever exercising an analysis. A minimal
  // valid payload unlocks the real flow.
  const { error: optErr } = await admin
    .from('experience_optimized_docs')
    .upsert({
      id: OPTIMIZED_DOC_ID,
      user_id: user.id,
      version: 1,
      payload: {
        summary: 'Full-stack engineer: TypeScript, React, Node.js, PostgreSQL.',
        roles: [],
        skills: [],
        outcomes: [],
        annotations: [],
      },
      source: 'llm',
    });
  if (optErr) throw new Error(`optimized doc upsert failed: ${optErr.message}`);

  const { error: targetErr } = await admin.from('targets').upsert({
    id: TARGET_ID,
    label: 'e2e-rendered-target',
    activation_status: 'ready',
    scoring_profile: {},
  });
  if (targetErr) throw new Error(`target upsert failed: ${targetErr.message}`);

  // user_targets has no reliable upsert conflict target across
  // environments — select-then-write keeps the link idempotent (mirrors
  // authed-filters-persist).
  const { data: existing, error: selErr } = await admin
    .from('user_targets')
    .select('id')
    .eq('user_id', user.id)
    .eq('target_id', TARGET_ID)
    .limit(1);
  if (selErr) throw new Error(`user_targets select failed: ${selErr.message}`);
  if (existing?.length) {
    const { error: updErr } = await admin
      .from('user_targets')
      .update({ is_active: true })
      .eq('user_id', user.id)
      .eq('target_id', TARGET_ID);
    if (updErr)
      throw new Error(`user_targets update failed: ${updErr.message}`);
  } else {
    const { error: linkErr } = await admin.from('user_targets').insert({
      user_id: user.id,
      target_id: TARGET_ID,
      is_active: true,
    });
    if (linkErr)
      throw new Error(`user_targets insert failed: ${linkErr.message}`);
  }

  const { error: srcErr } = await admin.from('sources').upsert({
    id: SOURCE_ID,
    board_token: 'e2e-rendered-board',
    company_name: 'E2E Rendered Corp',
    provider: 'greenhouse',
    enabled: false,
  });
  if (srcErr) throw new Error(`source upsert failed: ${srcErr.message}`);

  const { error: jobsErr } = await admin.from('jobs').upsert([
    {
      id: JOB_GRADED_ID,
      external_id: 'e2e-rendered-graded',
      source_id: SOURCE_ID,
      title: 'E2E Graded Platform Role',
      company_name: 'E2E Rendered Corp',
      location: 'Remote, US',
      is_us: true,
      description_html:
        '<p>Build and operate a web platform. TypeScript, React, Node.js, ' +
        'PostgreSQL. You will own features end to end and ship weekly.</p>',
      salary_min: 150000,
      salary_max: 180000,
      salary_currency: 'USD',
      salary_period: 'yearly',
    },
    {
      id: JOB_PENDING_ID,
      external_id: 'e2e-rendered-pending',
      source_id: SOURCE_ID,
      title: 'E2E Pending Platform Role',
      company_name: 'E2E Rendered Corp',
      location: 'Remote, US',
      is_us: true,
      description_html: '<p>Another platform role, not yet graded.</p>',
    },
  ]);
  if (jobsErr) throw new Error(`jobs upsert failed: ${jobsErr.message}`);

  // The graded row carries axis_scores — the signal column _is_pending
  // keys on. The pending row deliberately carries scoring_status 'stage2'
  // with NO graded-signal columns: the exact prod shape whose absence
  // from jest fixtures (which defaulted to 'complete') hid #603.
  //
  // `recency_score` MUST be seeded explicitly, not left to the trigger.
  // `scores_sync_denorm` sets `recency_score := COALESCE(NEW.recency_score,
  // NEW.score)` (#665), which only fills it on INSERT — on the UPDATE half of
  // this upsert the unsupplied column keeps its OLD value. The list renders
  // `recency_score`, not `score`, so once any recency sweep has aged this row
  // the assertions below would compare against a stale number forever: the
  // suite passes exactly once, on a virgin database. Seeding both keeps the
  // fixture idempotent and makes it fully determine what the UI shows.
  const { error: scoresErr } = await admin.from('scores').upsert(
    [
      {
        job_posting_id: JOB_GRADED_ID,
        target_id: TARGET_ID,
        score: 87,
        recency_score: 87,
        scoring_status: 'stage2',
        // Real fit-axis keys (title/skills/seniority/domain) — the graded
        // signal for _is_pending AND what the panel's axis breakdown
        // renders (#609). Keyword keys here would render the empty-axes
        // fallback and hide a broken FitAxisList from the suite.
        axis_scores: {
          title_fit: 90,
          skills_fit: 85,
          seniority_fit: 88,
          domain_fit: 85,
        },
        excluded: false,
      },
      {
        job_posting_id: JOB_PENDING_ID,
        target_id: TARGET_ID,
        score: 55,
        recency_score: 55,
        scoring_status: 'stage2',
        axis_scores: null,
        excluded: false,
      },
    ],
    { onConflict: 'job_posting_id,target_id' }
  );
  if (scoresErr) throw new Error(`scores upsert failed: ${scoresErr.message}`);
});

test('graded rows render their fit score; ungraded rows render the pending badge', async ({
  page,
}) => {
  await page.goto(`/jobs?target=${TARGET_ID}`);

  // The graded row shows its number — never the pending dot or an
  // in-flight spinner (prod regression #603: every cell spun forever).
  const gradedRow = page
    .getByRole('row')
    .filter({ hasText: 'E2E Graded Platform Role' });
  // Generous first-paint timeout: the /jobs first render sits behind a
  // serial auth-refresh → targets → status → jobs chain, and under full
  // parallel workers on one shared local stack that chain can exceed 30s.
  // Later asserts keep tight defaults.
  await expect(gradedRow.getByLabel('Match score 87')).toBeVisible({
    timeout: 60_000,
  });
  await expect(gradedRow.getByLabel(/scoring in progress/i)).toHaveCount(0);

  // The ungraded row shows the pending badge — never its keyword
  // placeholder number dressed up as a fit score (#47).
  const pendingRow = page
    .getByRole('row')
    .filter({ hasText: 'E2E Pending Platform Role' });
  await expect(pendingRow.getByLabel('Match score pending')).toBeVisible();
  await expect(pendingRow.getByLabel('Match score 55')).toHaveCount(0);

  // Structured salary renders through the shared formatter (#606) — one
  // format, k-compacted, no board-text passthrough.
  await expect(gradedRow.getByText('$150k–$180k')).toBeVisible();
});

test('the analysis panel survives verdict completion and its refetch (#602)', async ({
  page,
}) => {
  await page.goto(`/jobs?target=${TARGET_ID}`);

  const gradedRow = page
    .getByRole('row')
    .filter({ hasText: 'E2E Graded Platform Role' });
  await expect(gradedRow).toBeVisible({ timeout: 60_000 });
  await gradedRow.click();

  // Panel opens and the (mock-LLM) analysis auto-runs.
  await expect(page.getByText('Fit analysis')).toBeVisible();

  // Completion: the in-flight copy leaves. The mock provider grades fast;
  // the generous timeout covers a cold API.
  await expect(
    page.getByText('Analyzing this job against your profile')
  ).toBeHidden({ timeout: 90_000 });

  // The #602 contract: whatever the verdict did to the row's rank, the
  // panel the user is reading MUST still be mounted after the completion
  // refetch — either in place or pinned with the re-ranked notice.
  await expect(page.getByText('Fit analysis')).toBeVisible();
  await expect(page.getByRole('button', { name: /not for me/i })).toBeVisible();
});

test('a failed jobs fetch renders the load-error state with a working retry — never "No jobs found" (#604)', async ({
  page,
  allow5xx,
}) => {
  // Provoked failure: the tripwire fixture must not flag it.
  allow5xx(/\/api\/jobs/);
  let failing = true;
  await page.route('**/api/jobs?**', async route => {
    if (failing) {
      await route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({ error: 'WyrdFold API unavailable' }),
      });
      return;
    }
    await route.fallback();
  });

  await page.goto(`/jobs?target=${TARGET_ID}`);

  // Next's route announcer also carries role=alert — scope by copy.
  const alert = page.getByRole('alert').filter({ hasText: /loading problem/i });
  await expect(alert).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(/no jobs found/i)).toHaveCount(0);

  // Heal the network; Retry must recover to real rows without a reload.
  failing = false;
  await page.getByRole('button', { name: /retry/i }).click();
  await expect(page.getByText('E2E Graded Platform Role')).toBeVisible({
    timeout: 30_000,
  });
});

test('/onboarding redirects completed profiles to the dashboard (#607)', async ({
  page,
}) => {
  await page.goto('/onboarding');
  await expect(page).toHaveURL(/\/dashboard/, { timeout: 15_000 });
  await expect(page.getByText('Welcome to WyrdFold')).toHaveCount(0);
});
