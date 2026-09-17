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
_REPO_ROOT = _API_DIR.parents[1]  # apps/wyrdfold-api -> apps -> repo root


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
#
# "Build input" here means both:
#   * explicit COPY/ADD operands read from the build context, and
#   * the IMPLICIT inputs — the Dockerfile itself and the root .dockerignore,
#     which decides what is even present in the context. A .dockerignore edit
#     can exclude uv.lock or the app source and break the image without
#     touching any watched path.
#
# The parser below FAILS CLOSED. Any Dockerfile form it cannot decode raises
# rather than returning a short list, because silently under-reporting sources
# is the one failure mode that would make this guard worse than no guard: it
# would certify a watch set that misses a real input.
# ---------------------------------------------------------------------------

import json


class DockerfileParseError(Exception):
    """Raised for a Dockerfile form the guard cannot decode.

    Deliberately fatal: see the fail-closed note above.
    """


def _logical_lines(text: str) -> list[str]:
    """Dockerfile instructions with line continuations joined and comments dropped.

    A continued `COPY a \\ <newline> b ./` is ONE instruction. Parsing it
    line-by-line silently loses every operand after the first — which is
    exactly how a real build input becomes invisible to this guard.
    """
    lines: list[str] = []
    buf = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("#"):  # Docker drops comments, including mid-continuation
            continue
        buf = f"{buf} {stripped}" if buf else stripped
        if buf.endswith("\\"):
            buf = buf[:-1].rstrip()
            continue
        if buf:
            lines.append(buf)
        buf = ""
    if buf:
        lines.append(buf)
    return lines


def _dockerfile_context_sources() -> set[str]:
    """Paths COPY/ADDed from the BUILD CONTEXT (the repo), not from a stage.

    `COPY --from=...` pulls from another image or build stage, so it is not a
    repository input and must not be required of watchPatterns.
    """
    text = (_API_DIR / "Dockerfile").read_text(encoding="utf-8")
    sources: set[str] = set()

    for line in _logical_lines(text):
        head, _, rest = line.partition(" ")
        if head.upper() not in {"COPY", "ADD"}:
            continue
        if "<<" in rest:
            raise DockerfileParseError(
                f"heredoc form is not decoded by this guard: {line!r}. "
                "Extend _dockerfile_context_sources before using it."
            )

        operands = rest.strip()
        flags = []
        while operands.startswith("--"):
            flag, _, operands = operands.partition(" ")
            flags.append(flag)
            operands = operands.strip()
        if any(f.startswith("--from=") for f in flags):
            continue

        if operands.startswith("["):  # JSON form: COPY ["src", "dest"]
            try:
                parts = json.loads(operands)
            except ValueError as exc:
                raise DockerfileParseError(f"unparseable JSON-form operands: {line!r}") from exc
            if not isinstance(parts, list) or not all(isinstance(x, str) for x in parts):
                raise DockerfileParseError(f"unexpected JSON-form operands: {line!r}")
        else:
            parts = operands.split()

        if len(parts) < 2:
            raise DockerfileParseError(f"expected at least a source and a destination: {line!r}")
        sources.update(parts[:-1])  # last operand is the destination inside the image

    return sources


def _implicit_build_inputs() -> set[str]:
    """Files that shape the image without ever appearing as a COPY operand."""
    config = tomllib.loads((_API_DIR / "railway.toml").read_text(encoding="utf-8"))
    dockerfile_path = config.get("build", {}).get("dockerfilePath")
    inputs = {".dockerignore"}  # decides what is present in the context at all
    if dockerfile_path:
        inputs.add(dockerfile_path)
    return inputs


def _watch_patterns() -> list[str]:
    config = tomllib.loads((_API_DIR / "railway.toml").read_text(encoding="utf-8"))
    return config.get("build", {}).get("watchPatterns") or []


def _covered(path: str, patterns: list[str]) -> bool:
    """Does any watch pattern match this repo path?"""
    for pat in patterns:
        if pat == path:
            return True
        if pat.endswith("/**") and (path.startswith(pat[:-3] + "/") or path == pat[:-3]):
            return True
    return False


def test_watch_patterns_cover_every_dockerfile_build_input() -> None:
    """The bug this encodes: the root uv.lock is a build input that lived
    outside the watched directory, so dependency bumps never redeployed."""
    patterns = _watch_patterns()
    assert patterns, "railway.toml declares no watchPatterns — nothing constrains rebuilds"

    sources = _dockerfile_context_sources()
    assert sources, "parsed no COPY sources from the Dockerfile — the parser is broken"

    required = sources | _implicit_build_inputs()
    uncovered = sorted(s for s in required if not _covered(s, patterns))
    assert not uncovered, (
        f"These are Docker build inputs, but no watchPattern matches them: {uncovered}. "
        f"Railway would report SKIPPED for a release that changed only those files, and "
        f"the image would never rebuild — silently, because SKIPPED is a success state. "
        f"Add them to [build].watchPatterns in railway.toml."
    )


def test_the_root_lockfile_is_watched() -> None:
    """Named explicitly because it is the one that actually bit: a dependency
    bump touches ONLY the root uv.lock."""
    patterns = _watch_patterns()
    assert _covered("uv.lock", patterns), "root uv.lock is not watched"
    assert _covered("pyproject.toml", patterns), "root pyproject.toml is not watched"


def test_the_root_dockerignore_is_watched() -> None:
    """.dockerignore never appears as a COPY operand, but it decides what is
    present in the build context — an edit can exclude uv.lock or the app
    source and break the image while matching no other watched path."""
    assert (_REPO_ROOT / ".dockerignore").is_file(), "expected a root .dockerignore"
    assert _covered(".dockerignore", _watch_patterns()), "root .dockerignore is not watched"


def test_the_service_directory_is_still_watched() -> None:
    """Guards the additions above against having replaced the original cover
    rather than extended it."""
    patterns = _watch_patterns()
    assert _covered("apps/wyrdfold-api/app", patterns)
    assert _covered("apps/wyrdfold-api/Dockerfile", patterns)


def test_railway_toml_watches_itself() -> None:
    """Otherwise a change to the deploy config cannot trigger the deploy that
    applies it — which is the position this repository was in when the gap was
    found."""
    assert _covered("apps/wyrdfold-api/railway.toml", _watch_patterns())


def test_parser_decodes_continuation_and_json_forms() -> None:
    """The parser is the guard's weakest link: if it under-reports sources, the
    coverage test passes while a real build input goes unwatched. A continued
    COPY silently lost every operand after the first before this was fixed."""
    probe = (
        "FROM scratch\n"
        'COPY ["json_a.txt", "json_b.txt", "./"]\n'
        "COPY cont_a.txt \\\n"
        "     # a comment mid-continuation\n"
        "     cont_b.txt ./\n"
        "COPY --chown=app:app plain.txt ./\n"
        "ADD added.txt ./\n"
        "COPY --from=builder /not/a/repo/path ./\n"
    )
    dockerfile = _API_DIR / "Dockerfile"
    original = dockerfile.read_text(encoding="utf-8")
    try:
        dockerfile.write_text(probe, encoding="utf-8")
        got = _dockerfile_context_sources()
    finally:
        dockerfile.write_text(original, encoding="utf-8")

    assert got == {
        "json_a.txt",
        "json_b.txt",
        "cont_a.txt",
        "cont_b.txt",
        "plain.txt",
        "added.txt",
    }, f"parser mis-decoded the probe: {sorted(got)}"


def test_parser_fails_closed_on_forms_it_cannot_decode() -> None:
    """Returning a short list for an unknown form would certify a watch set
    that misses a real input. Raising is the safe direction."""
    dockerfile = _API_DIR / "Dockerfile"
    original = dockerfile.read_text(encoding="utf-8")
    for bad in ("COPY <<EOF /app/f\nbody\nEOF\n", "COPY [\n", "COPY only_one_operand\n"):
        try:
            dockerfile.write_text(f"FROM scratch\n{bad}", encoding="utf-8")
            try:
                _dockerfile_context_sources()
            except DockerfileParseError:
                continue
            raise AssertionError(f"parser silently accepted an undecodable form: {bad!r}")
        finally:
            dockerfile.write_text(original, encoding="utf-8")
