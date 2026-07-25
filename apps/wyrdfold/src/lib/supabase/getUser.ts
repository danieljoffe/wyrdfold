import { cache } from 'react';
import type { User } from '@supabase/supabase-js';

import { createAuthServerClient } from './auth-server';

/**
 * The current Supabase user (or `null` when logged-out), for Server Components
 * that need to branch on auth WITHOUT gating — the auth-adaptive `/search`
 * surface picks its shell + rendering from this (#467 §10). Distinct from the
 * proxy's `getAccessToken` (which returns the token for upstream Bearer calls);
 * here we only need the identity/presence.
 *
 * Wrapped in React `cache()` so a layout and its page share ONE JWT
 * verification within a single request render — `/search`'s layout (shell
 * choice) and page (rendering) both call this, and cache() collapses them to a
 * single `auth.getUser()` round-trip. Never throws: a misconfig / network
 * failure resolves to `null` (treated as logged-out — fail-closed to the public
 * surface, never a crash).
 */
export const getOptionalUser = cache(async (): Promise<User | null> => {
  try {
    const supabase = await createAuthServerClient();
    const {
      data: { user },
    } = await supabase.auth.getUser();
    return user ?? null;
  } catch {
    return null;
  }
});
