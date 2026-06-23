import { createClient, type SupabaseClient } from '@supabase/supabase-js';

/**
 * Service-role Supabase client for trusted server-side writes that must
 * bypass RLS (e.g. the public waitlist insert into an otherwise
 * deny-all table). This client wields the service-role key — it MUST
 * NEVER be imported into a Client Component or shipped to the browser.
 *
 * The only legitimate importer is a Route Handler (`/api/waitlist`), which
 * runs server-side. RLS does not protect this client — every caller is fully
 * privileged — so each call site is responsible for its own validation,
 * authorization, and rate-limiting.
 *
 * The `SUPABASE_SERVICE_ROLE_KEY` env var is intentionally NOT
 * `NEXT_PUBLIC_`-prefixed, so Next.js will not inline it into the client
 * bundle. The `typeof window` guard below is belt-and-suspenders: if this
 * module is ever wrongly pulled into client code, it throws at runtime
 * instead of silently exposing the key access path.
 *
 * Auth persistence is disabled: there is no user session here, just a
 * privileged key, so token refresh / session storage would be pointless
 * (and in a serverless route, harmful — sessions don't survive invocations).
 */
export function createServiceRoleClient(): SupabaseClient {
  if (typeof window !== 'undefined') {
    throw new Error(
      'createServiceRoleClient() must never run in the browser — it uses the service-role key'
    );
  }

  const supabaseUrl = process.env['NEXT_PUBLIC_SUPABASE_URL'];
  const serviceRoleKey = process.env['SUPABASE_SERVICE_ROLE_KEY'];

  if (!supabaseUrl || !serviceRoleKey) {
    throw new Error(
      'Missing NEXT_PUBLIC_SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY environment variables'
    );
  }

  return createClient(supabaseUrl, serviceRoleKey, {
    auth: {
      autoRefreshToken: false,
      persistSession: false,
    },
  });
}
