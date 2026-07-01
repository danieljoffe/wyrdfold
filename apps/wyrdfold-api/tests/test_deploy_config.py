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
