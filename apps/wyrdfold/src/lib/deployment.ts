/**
 * Deployment-mode awareness (docs/plan-wyrdfold-deployment-modes.md, Phase 2).
 *
 * Mirrors the API's DEPLOYMENT_MODE: the mode gates only perimeter
 * *presentation* here — in `self_host` the public homepage drops the waitlist
 * funnel for a direct sign-in CTA (a self-hosted instance has no waitlist).
 * Auth mechanics are identical in both modes.
 *
 * Default `self_host` — the safe posture for anyone cloning the repo — same
 * default as the API. The hosted (saas) deployment must set
 * `NEXT_PUBLIC_DEPLOYMENT_MODE=saas` in its env.
 *
 * Read at call time (not module scope) so server components re-evaluate per
 * render and tests can flip the env per case.
 */
export type DeploymentMode = 'self_host' | 'saas';

export function deploymentMode(): DeploymentMode {
  const raw = process.env['NEXT_PUBLIC_DEPLOYMENT_MODE'];
  // Unset = self_host, the documented default. Anything else must be an
  // exact mode: a typo ('sass', 'SAAS') silently defaulting would flip the
  // hosted homepage off the waitlist funnel with no signal — fail loudly
  // instead, so a bad deploy env 500s and gets caught by the deploy smoke.
  if (raw === undefined || raw === '') {
    return 'self_host';
  }
  if (raw !== 'saas' && raw !== 'self_host') {
    throw new Error(
      `NEXT_PUBLIC_DEPLOYMENT_MODE must be "self_host" or "saas", got "${raw}"`
    );
  }
  return raw;
}
