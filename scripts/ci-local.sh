#!/usr/bin/env bash
#
# Local mirror of .github/workflows/ci.yml — run the CI checks on your machine.
# Handy when GitHub Actions is unavailable, or as a fast pre-push gate.
#
# Usage:
#   scripts/ci-local.sh                 # run every available job
#   scripts/ci-local.sh js python       # run only the named jobs
#
# Jobs:  js  python  e2e  lighthouse  trivy
#   - Only `trivy` needs Docker; it is skipped automatically when Docker
#     isn't installed/running.
#   - `python` shells out to pandoc for some render tests: brew install pandoc
#   - `e2e` runs in CI mode (chromium-only public specs, prod server); install
#     the browser once: pnpm exec playwright install chromium
#
# Unlike CI, this keeps going after a failing job so you see everything in one
# pass; it exits non-zero if any job failed.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Run in CI mode so this faithfully mirrors GitHub Actions rather than local
# dev behavior. Concretely: the e2e suite runs chromium-only public specs
# against the built prod server (the dev matrix also spins up firefox/webkit,
# which you'd otherwise have to install), and the wyrdfold build skips Sentry
# source-map upload. Must be exported before any job runs.
export CI=1

if [ "$#" -gt 0 ]; then JOBS=("$@"); else JOBS=(js python e2e lighthouse trivy); fi

results=()
overall=0

want() { local j; for j in "${JOBS[@]}"; do [ "$j" = "$1" ] && return 0; done; return 1; }
have() { command -v "$1" >/dev/null 2>&1; }
hdr()  { printf '\n\033[1;34m▶ %s\033[0m\n' "$1"; }

# run <label> <cmd...> — execute, record pass/fail, never abort the script.
run() {
  local label="$1"; shift
  hdr "$label"
  if "$@"; then results+=("✅ $label"); else results+=("❌ $label"); overall=1; fi
}

# --- JS: lint, typecheck, test, build --------------------------------------
if want js; then
  run "JS · install" pnpm install --frozen-lockfile
  run "JS · audit"   pnpm audit --prod
  run "JS · lint/typecheck/test/build" \
    pnpm nx run-many -t lint typecheck test build --exclude=wyrdfold-api --nxBail
fi

# --- Python: ruff, mypy, pytest --------------------------------------------
if want python; then
  have pandoc || echo "⚠  pandoc not found — some render tests will fail (brew install pandoc)"
  run "PY · uv sync" uv sync --frozen
  run "PY · ruff"    bash -c 'cd apps/wyrdfold-api && uv run --package wyrdfold-api ruff check .'
  run "PY · mypy"    bash -c 'cd apps/wyrdfold-api && uv run --package wyrdfold-api mypy app/'
  run "PY · pytest"  bash -c 'cd apps/wyrdfold-api && uv run --package wyrdfold-api pytest -v'
fi

# --- Build (shared by e2e + lighthouse) ------------------------------------
if want e2e || want lighthouse; then
  run "Build · wyrdfold (prod)" pnpm nx build wyrdfold --prod
fi

# --- E2E: Playwright public specs ------------------------------------------
if want e2e; then
  run "E2E · playwright" pnpm nx e2e wyrdfold-e2e
fi

# --- Lighthouse: public CWV / a11y -----------------------------------------
if want lighthouse; then
  run "Lighthouse · lhci" pnpm exec lhci autorun
fi

# --- Trivy: image scan (Docker only) ---------------------------------------
if want trivy; then
  if have docker && docker info >/dev/null 2>&1; then
    run "Trivy · build image" \
      docker build -f apps/wyrdfold-api/Dockerfile -t wyrdfold-api:ci .
    run "Trivy · scan" \
      docker run --rm -v "$PWD/.trivyignore:/.trivyignore:ro" aquasec/trivy:0.69.1 image \
        --severity HIGH,CRITICAL --vuln-type os,library --ignore-unfixed \
        --ignorefile /.trivyignore --exit-code 1 wyrdfold-api:ci
  else
    hdr "Trivy · image scan"
    echo "⏭  skipped — Docker not installed/running"
    results+=("⏭  Trivy (skipped: no Docker)")
  fi
fi

# --- Summary ----------------------------------------------------------------
printf '\n\033[1m==== ci-local summary ====\033[0m\n'
printf '%s\n' "${results[@]}"
exit "$overall"
