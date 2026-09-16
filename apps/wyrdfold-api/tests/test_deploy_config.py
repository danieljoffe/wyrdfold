"""Deploy-config invariants (#29 C1).

The container start command lives ONLY in the Dockerfile ``CMD`` now —
``railway.toml``'s ``[deploy].startCommand`` was removed because it ran
``uv run`` (uv is builder-only, absent from the runtime image → ``uv: not
found``) and it dropped ``--proxy-headers``. These spend-free checks pin the
invariants so that drift can't silently return:

- the Dockerfile ``CMD`` keeps ``--proxy-headers`` — without it uvicorn trusts
  the LB IP for every request, collapsing the pre-auth ``slowapi`` rate-limit
  buckets into one shared bucket; and
- ``railway.toml`` does not re-introduce a ``[deploy].startCommand``, which
  would override the ``CMD`` and could re-drift / reintroduce the break.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

_API_DIR = Path(__file__).resolve().parents[1]


def test_dockerfile_cmd_is_the_start_command_with_proxy_headers() -> None:
    dockerfile = (_API_DIR / "Dockerfile").read_text(encoding="utf-8")
    # The final CMD is the sole start command; --proxy-headers is load-bearing
    # for correct client IPs behind Railway's LB (rate limiting keys on them).
    assert "uvicorn app.main:app" in dockerfile
    assert "--proxy-headers" in dockerfile
    assert "--forwarded-allow-ips" in dockerfile


def test_railway_toml_has_no_deploy_start_command() -> None:
    config = tomllib.loads((_API_DIR / "railway.toml").read_text(encoding="utf-8"))
    # A [deploy].startCommand overrides the Dockerfile CMD; re-adding one reopens
    # the #29 C1 drift (uv-not-found / dropped --proxy-headers). Also asserts the
    # file is still valid TOML.
    assert "startCommand" not in config.get("deploy", {})
    # The Dockerfile builder is still the (correct) build path.
    assert config.get("build", {}).get("builder") == "DOCKERFILE"


# ---------------------------------------------------------------------------
# Watch paths must cover every build input.
#
# Railway rebuilds only when a changed file matches `[build].watchPatterns`.
# The Docker build context is the MONOREPO ROOT, so the image depends on files
# outside apps/wyrdfold-api/ — and before this guard the filter watched only
# the service directory.
#
# The consequence was invisible: a Python dependency bump changes the root
# uv.lock and nothing else, so Railway reported SKIPPED (a SUCCESS state), the
# API stayed healthy, and /version reported the older commit — which reads as
# "that release had no API changes" rather than "the API change never shipped".
# On 2026-09-16 `main` said anthropic 1.5.0 while production ran an image built
# from a tree containing 1.4.0.
#
# These tests derive the requirement from the Dockerfile itself rather than
# restating it, so a NEW build input cannot be added without either extending
# watchPatterns or failing here.
# ---------------------------------------------------------------------------


def _dockerfile_context_sources() -> set[str]:
    """Paths COPYed from the BUILD CONTEXT (i.e. the repo), not from a stage.

    `COPY --from=...` pulls from another image or build stage, so it is not a
    repository input and must not be required of watchPatterns.
    """
    text = (_API_DIR / "Dockerfile").read_text(encoding="utf-8")
    sources: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith("COPY "):
            continue
        if "--from=" in stripped:
            continue
        parts = [p for p in stripped.split()[1:] if not p.startswith("--")]
        # the last token is the destination inside the image
        sources.update(parts[:-1])
    return sources


def _covered(path: str, patterns: list[str]) -> bool:
    """Does any watch pattern match this repo path?"""
    for pat in patterns:
        if pat == path:
            return True
        if pat.endswith("/**") and path.startswith(pat[:-3] + "/"):
            return True
        if pat.endswith("/**") and path == pat[:-3]:
            return True
    return False


def test_watch_patterns_cover_every_dockerfile_build_input() -> None:
    """The bug this encodes: the root uv.lock is a build input that lived
    outside the watched directory, so dependency bumps never redeployed."""
    config = tomllib.loads((_API_DIR / "railway.toml").read_text(encoding="utf-8"))
    patterns = config.get("build", {}).get("watchPatterns")
    assert patterns, "railway.toml declares no watchPatterns — every path is watched or none is"

    sources = _dockerfile_context_sources()
    assert sources, "parsed no COPY sources from the Dockerfile — the parser is broken"

    uncovered = sorted(s for s in sources if not _covered(s, patterns))
    assert not uncovered, (
        f"Dockerfile COPYs {uncovered} from the build context, but no watchPattern "
        f"matches them. Railway would report SKIPPED for a release that changed only "
        f"those files, and the image would never rebuild — silently, because SKIPPED "
        f"is a success state. Add them to [build].watchPatterns in railway.toml."
    )


def test_the_root_lockfile_is_watched() -> None:
    """Named explicitly because it is the one that actually bit: a dependency
    bump touches ONLY the root uv.lock."""
    config = tomllib.loads((_API_DIR / "railway.toml").read_text(encoding="utf-8"))
    patterns = config.get("build", {}).get("watchPatterns", [])
    assert _covered("uv.lock", patterns), "root uv.lock is not watched"
    assert _covered("pyproject.toml", patterns), "root pyproject.toml is not watched"


def test_the_service_directory_is_still_watched() -> None:
    """Guards the additions above against having replaced the original cover
    rather than extended it."""
    config = tomllib.loads((_API_DIR / "railway.toml").read_text(encoding="utf-8"))
    patterns = config.get("build", {}).get("watchPatterns", [])
    assert _covered("apps/wyrdfold-api/app", patterns)
    assert _covered("apps/wyrdfold-api/Dockerfile", patterns)


def test_railway_toml_watches_itself() -> None:
    """Otherwise a change to the deploy config cannot trigger the deploy that
    applies it — which is the position this repository was in when the gap was
    found."""
    config = tomllib.loads((_API_DIR / "railway.toml").read_text(encoding="utf-8"))
    patterns = config.get("build", {}).get("watchPatterns", [])
    assert _covered("apps/wyrdfold-api/railway.toml", patterns)
