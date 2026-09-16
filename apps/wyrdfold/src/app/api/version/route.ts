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

// `environment` names THIS deployment's environment, and it cannot come from
// VERCEL_ENV. Vercel sets that to 'production' for the production deployment of
// ANY project, so the staging frontend reported `environment: production` —
// true about the staging project's own tier, and useless for the question
// anyone is actually asking (#1053). Meanwhile the production API reported
// `development`, so the two labels lied in opposite directions.
//
// NEXT_PUBLIC_ENV_NAME is set per Vercel project and inlined at build time, so
// it travels with the artifact exactly like NEXT_PUBLIC_BUILD_SHA above.
//
// NO FALLBACK TO VERCEL_ENV. Falling back would reinstate the wrong answer
// whenever the variable is missing — and a wrong label is worse than none,
// because it is believed. Unset reports null, the same way a missing build SHA
// does: absence of proof, stated as absence.
export function GET() {
  return NextResponse.json({
    commit: process.env.NEXT_PUBLIC_BUILD_SHA ?? null,
    environment: process.env.NEXT_PUBLIC_ENV_NAME ?? null,
  });
}
