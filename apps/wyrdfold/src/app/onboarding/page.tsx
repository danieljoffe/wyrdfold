import type { Metadata } from 'next';
import { redirect } from 'next/navigation';
import { createAuthServerClient } from '@/lib/supabase/auth-server';
import { fetchJsonFromWyrdfoldAPI } from '@/lib/api/proxy';
import OnboardingWizard from './OnboardingWizard';

export const metadata: Metadata = {
  title: 'Get Started',
};

// Dynamic-rendering boundary is now signalled by `await connection()` inside
// createAuthServerClient (see lib/supabase/auth-server.ts), so the explicit
// force-dynamic export is no longer required.

export default async function OnboardingPage() {
  const supabase = await createAuthServerClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();

  if (!user) {
    redirect('/login');
  }

  // Resume support (#85): the wizard reads the persisted path + step so a
  // user who dropped out mid-flow continues where they left off rather than
  // restarting from the top. Null on a first run or a degraded read → the
  // wizard starts cleanly at the path chooser.
  const onboarding = await fetchJsonFromWyrdfoldAPI<{
    completed_at: string | null;
    path: 'A' | 'B' | 'C' | null;
    current_step: string | null;
  }>('/profile/onboarding');

  return (
    <OnboardingWizard
      initialPath={onboarding?.path ?? null}
      initialStep={onboarding?.current_step ?? null}
    />
  );
}
