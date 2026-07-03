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
  return process.env['NEXT_PUBLIC_DEPLOYMENT_MODE'] === 'saas'
    ? 'saas'
    : 'self_host';
}
