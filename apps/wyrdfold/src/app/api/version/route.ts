import { NextResponse } from 'next/server';

// Build-provenance marker (#976): proves the DEPLOYED frontend corresponds
// to the release commit, mirroring the API's `/version` (Railway-injected
// SHA). Vercel is not git-connected — every release runs `vercel --prod`
// from a local checkout — so the release skill injects the SHA at deploy
// time: `--build-env NEXT_PUBLIC_BUILD_SHA=$(git rev-parse HEAD)`.
//
// NEXT_PUBLIC_* is inlined by the compiler at build time, so the value is
// baked into the artifact itself; a deploy that skipped the flag reports
// `commit: null` rather than a stale or guessed value — the post-deploy
// smoke treats null as a failed provenance check, not a pass. Public by
// design, like the API's `/version`: a commit SHA of a public repo reveals
// nothing.

export const dynamic = 'force-dynamic';

export function GET() {
  return NextResponse.json({
    commit: process.env.NEXT_PUBLIC_BUILD_SHA ?? null,
    environment: process.env.VERCEL_ENV ?? null,
  });
}
