// Idempotently create the e2e test user against the LOCAL Supabase stack.
//
// CI boots a throwaway `supabase start` stack (zero secrets — the local dev
// keys are public defaults), so the auth fixture's user has to be created
// fresh every run. `auth.setup.ts` then mints a magic-link OTP for this user
// via the admin API and exchanges it for a real session (the app is
// passwordless, so this mirrors the production sign-in path exactly).
//
// Local dev: safe to run against your own stack too — "already registered"
// is treated as success.
import { createClient } from '@supabase/supabase-js';

const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
const serviceRoleKey = process.env.SUPABASE_SERVICE_ROLE_KEY;
const email = process.env.E2E_TEST_USER_EMAIL;

if (!url || !serviceRoleKey || !email) {
  console.error(
    'seed-e2e-user: NEXT_PUBLIC_SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY and ' +
      'E2E_TEST_USER_EMAIL must all be set.'
  );
  process.exit(1);
}

const admin = createClient(url, serviceRoleKey, {
  auth: { autoRefreshToken: false, persistSession: false },
});

const { data, error } = await admin.auth.admin.createUser({
  email,
  email_confirm: true,
});

let userId = data?.user?.id;
if (error) {
  // Idempotency: a rerun against a stack that already has the user is fine.
  if (/already.*(registered|exists)/i.test(error.message)) {
    console.log(`seed-e2e-user: ${email} already exists — ok.`);
    const { data: list } = await admin.auth.admin.listUsers();
    userId = list?.users?.find((u) => u.email === email)?.id;
  } else {
    console.error(`seed-e2e-user: failed to create ${email}: ${error.message}`);
    process.exit(1);
  }
} else {
  console.log(`seed-e2e-user: created ${email} (${userId}).`);
}

if (!userId) {
  console.error('seed-e2e-user: could not resolve the user id.');
  process.exit(1);
}

// Mark the user as PAST onboarding. The dashboard redirects any profile whose
// `onboarding_completed_at` is null into the wizard, and the authed smoke
// specs assert the *established-user* surface (dashboard chrome, sidebar,
// profile page) — the wizard itself has its own specs. Upsert keeps reruns
// idempotent.
const { error: profileError } = await admin.from('user_profiles').upsert(
  {
    user_id: userId,
    name: 'E2E Test User',
    onboarding_completed_at: new Date().toISOString(),
  },
  { onConflict: 'user_id' }
);
if (profileError) {
  console.error(`seed-e2e-user: profile upsert failed: ${profileError.message}`);
  process.exit(1);
}
console.log('seed-e2e-user: profile marked onboarded.');

// Seed a minimal master prose doc. The /profile page renders its
// empty-state CTA (no identity card, no Name field) until the user has
// authored experience content — the authed specs assert the
// established-user surface. Insert once; reruns skip.
const { data: existingProse } = await admin
  .from('experience_prose_docs')
  .select('id')
  .eq('user_id', userId)
  .limit(1);
if (!existingProse?.length) {
  const { error: proseError } = await admin.from('experience_prose_docs').insert({
    user_id: userId,
    version: 1,
    content:
      'E2E seeded experience document. Senior engineer with ten years of shipped work.',
  });
  if (proseError) {
    console.error(`seed-e2e-user: prose seed failed: ${proseError.message}`);
    process.exit(1);
  }
  console.log('seed-e2e-user: prose doc seeded.');
} else {
  console.log('seed-e2e-user: prose doc already present — ok.');
}
