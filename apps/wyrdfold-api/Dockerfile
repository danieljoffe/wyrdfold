# syntax=docker/dockerfile:1.7

# ---- builder ----------------------------------------------------------------
# Resolves the uv-managed virtualenv into /app/.venv. uv + the lockfile + the
# build-context pyproject files only live in this stage; the runtime image
# never sees them.
FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4 AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Build context is the monorepo root so the workspace lockfile is available.
COPY pyproject.toml uv.lock ./
COPY apps/wyrdfold-api/pyproject.toml ./apps/wyrdfold-api/pyproject.toml

# No BuildKit cache mount here, deliberately. Railway's builder keeps
# moving the goalposts on cache-mount ids: it rejected the shorthand
# `--mount=type=cache,target=...` ("missing an id argument"), then rejected
# an explicit literal id ("missing the cacheKey prefix from its id" — it
# wants a service-scoped prefix we can't hardcode without embedding an
# account/service UUID). A cache mount is only a rebuild-speed optimization
# (uv re-fetches wheels each build otherwise, and uv is fast), so we drop it
# entirely to keep the Dockerfile valid on Railway, locally, and any builder.
RUN uv sync --frozen --no-dev --no-editable --package wyrdfold-api

# ---- runtime ----------------------------------------------------------------
FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4 AS runtime

# pandoc is shelled out to by the DOCX render pipeline; the rest of the
# runtime stays bare (no uv, no apt cache, no monorepo lockfiles).
#
# ``apt-get upgrade`` applies Debian's security patches at build time. Pinning
# the base by digest makes the base reproducible, but it also FREEZES its OS
# packages at whatever shipped that day — so a CVE fixed in Debian after the
# digest was cut stays in our image until a new base is published and
# Dependabot bumps it. That gap is not hypothetical: the util-linux family
# (bsdutils and siblings) carried CVE-2026-53615 as 9 HIGH findings while
# Debian had already published the fix in 2.41.5-0+deb13u1, and the image was
# serving production the whole time.
#
# The trade is that this layer floats: same Dockerfile, different packages as
# Debian publishes fixes. That is the correct direction for a runtime — the
# BASE stays pinned and reproducible, security patches do not wait on a digest
# bump.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends pandoc \
    && rm -rf /var/lib/apt/lists/*

# Strip the base image's pip / ensurepip. The app runs entirely out of the
# uv-built venv at /app/.venv (its own site-packages, base interpreter only),
# so nothing here installs packages at runtime — and an image that can't
# `pip install` is one an attacker who lands RCE can't extend either.
#
# It also closes a recurring false-positive channel in the Trivy gate: pip
# vendors ~20 libraries under `pip/_vendor/`, and Trivy reports CVEs against
# those vendored copies even though the runtime never invokes pip. The
# python:3.14.6→3.14.7 base bump (pip 26.1.2→26.2.1) fired exactly that way,
# on vendored msgpack 1.1.2 (GHSA-6v7p-g79w-8964) and setuptools 70.3.0
# (CVE-2025-47273). Deleting pip fixes the finding instead of ignoring it,
# and keeps future pip-vendored advisories from re-blocking base bumps.
RUN rm -rf /usr/local/lib/python3.14/site-packages/pip \
    /usr/local/lib/python3.14/site-packages/pip-*.dist-info \
    /usr/local/lib/python3.14/ensurepip \
    /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.14

RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --home-dir /app --shell /sbin/nologin app

WORKDIR /app

# Copy the resolved venv (built in the builder stage) + the app source.
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app apps/wyrdfold-api/app ./apps/wyrdfold-api/app

USER app

WORKDIR /app/apps/wyrdfold-api

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8001

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8001\")}/health', timeout=3).status == 200 else 1)"

# Default trusts proxy headers from any upstream — correct for Railway
# (their LB fronts every request). Self-hosters behind a known proxy
# should pin to its IPs by setting FORWARDED_ALLOW_IPS in their env. #30 F4.
ENV FORWARDED_ALLOW_IPS="*"

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips=${FORWARDED_ALLOW_IPS}"]
