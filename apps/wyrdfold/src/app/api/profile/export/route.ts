import { proxyToWyrdfoldAPI } from '@/lib/api/proxy';

// Personal-data export / portability (#81 / #29 P2). The wyrdfold-api's
// GET /profile/export returns a ZIP of every per-user row + uploaded
// files. `binary: true` streams the bytes through with the upstream
// `Content-Type: application/zip` and `Content-Disposition` filename
// preserved (same passthrough used for `.docx` and resume-zip exports),
// and `proxyToWyrdfoldAPI` forwards the user's verified Supabase JWT as
// Bearer auth — so a non-logged-in caller gets a 401, never the ZIP.
export async function GET() {
  return proxyToWyrdfoldAPI('/profile/export', { binary: true });
}
