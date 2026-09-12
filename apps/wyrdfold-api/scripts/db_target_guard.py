"""Refuse a remote Supabase write unless the operator NAMES the target.

WHY THIS EXISTS
``supabase db push`` writes to whatever project the CLI happens to be linked
to, and the link is sticky: ``supabase link`` persists it across sessions, in a
gitignored file nobody looks at. Today that link points at the project serving
production, which is itself named "WYRDFOLD dev" — so the one signal an
operator might glance at actively misleads them.

Adding a staging project (#1014) makes that materially worse, because it turns
a latent hazard into a routine one: the same command, typed the same way, now
means different things depending on invisible state from a previous session.

So this inverts it. The target is named ON THE COMMAND LINE, the link is read
and compared, and any disagreement refuses. Getting it wrong requires typing
``--target production`` while meaning staging — a mistake no amount of tooling
prevents — rather than merely forgetting which database you linked last week.

This is the "difficult by construction" half of #1014's acceptance criteria.
It is complementary to ``preflight_migrations.py``, which answers a different
question (is the target already migrated for the code being deployed) and runs
at a different time.

FAILS CLOSED, ALWAYS
Every unresolved condition refuses:

  * no ``--target``                  -> refuse (the whole point)
  * unknown target name              -> refuse, and list the declared ones
  * no link at all                   -> refuse, say how to link
  * link disagrees with the target   -> refuse, name both
  * duplicate/ambiguous declaration  -> refuse rather than guess
  * anything unexpected              -> refuse

That list is deliberate. The lesson from #1028 is that a gate which returns
success for conditions it did not anticipate is worse than no gate, because it
converts "we didn't check" into "we checked and it's fine". Project refs are
not secrets — the production ref is already committed in ``config.toml`` — so
refusals can name them, which is what makes them actionable.

USAGE
    # from the repo root, via the pnpm scripts that wrap this
    pnpm db:push --target production
    pnpm db:push --target staging

    # directly
    uv run --package wyrdfold-api python scripts/db_target_guard.py \
        --target production -- db push
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

# The repo root, from this file's location: apps/wyrdfold-api/scripts/ -> ../../..
REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_TOML = REPO_ROOT / "supabase" / "config.toml"
LINK_FILE = REPO_ROOT / "supabase" / ".temp" / "project-ref"

# ``[remotes.<name>]`` opens a block; ``project_id = "<ref>"`` inside it names
# the project. Matched narrowly on purpose — a loose parse that silently picked
# up a project_id from some OTHER section would defeat the entire check, so
# anything not matching this exact shape simply is not a declared target.
_REMOTE_HEADER = re.compile(r"^\s*\[remotes\.([A-Za-z0-9_-]+)\]\s*$")
_ANY_HEADER = re.compile(r"^\s*\[")
_PROJECT_ID = re.compile(r"""^\s*project_id\s*=\s*["']([A-Za-z0-9]+)["']\s*(?:#.*)?$""")


class GuardError(Exception):
    """A refusal. The message is written for the operator, not for a log."""


def declared_targets(config_text: str) -> dict[str, str]:
    """Map ``{target name: project ref}`` from ``[remotes.*]`` blocks.

    A target counts only when its block declares a ``project_id`` directly —
    nested tables like ``[remotes.production.auth]`` are not targets and must
    not contribute one. Two blocks declaring the same name is ambiguous, and
    ambiguity refuses rather than picking a winner.
    """
    targets: dict[str, str] = {}
    current: str | None = None
    for line in config_text.splitlines():
        header = _REMOTE_HEADER.match(line)
        if header:
            name = header.group(1)
            if name in targets:
                raise GuardError(
                    f"supabase/config.toml declares [remotes.{name}] more than once. "
                    "Refusing rather than guessing which one is authoritative."
                )
            current = name
            continue
        if _ANY_HEADER.match(line):
            # Any other header (including [remotes.x.auth]) closes the block.
            current = None
            continue
        if current is None:
            continue
        found = _PROJECT_ID.match(line)
        if found:
            targets[current] = found.group(1)
            current = None
    return targets


def linked_ref(link_file: Path) -> str | None:
    """The project ref the CLI is currently linked to, or ``None``."""
    try:
        value = link_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as exc:  # unreadable is NOT "unlinked" — surface it
        raise GuardError(f"could not read {link_file}: {exc}") from exc
    return value or None


def resolve(target: str, targets: dict[str, str], linked: str | None) -> str:
    """Return the ref to operate on, or raise :class:`GuardError`."""
    if not targets:
        raise GuardError(
            "supabase/config.toml declares no [remotes.<name>] targets, so there is "
            "nothing to name. Add one before using a targeted command."
        )
    if target not in targets:
        known = ", ".join(sorted(targets)) or "(none)"
        raise GuardError(
            f"unknown target {target!r}. Declared targets: {known}. "
            "Add a [remotes.<name>] block to supabase/config.toml first."
        )
    expected = targets[target]
    if linked is None:
        raise GuardError(
            f"the Supabase CLI is not linked to any project, so {target!r} cannot be "
            f"verified.\n  Run: supabase link --project-ref {expected}"
        )
    if linked != expected:
        by_ref = {ref: name for name, ref in targets.items()}
        actually = by_ref.get(linked, "a project not declared in config.toml")
        raise GuardError(
            f"REFUSING: you asked for {target!r} but the CLI is linked to {actually}.\n"
            f"  --target {target} expects: {expected}\n"
            f"  currently linked:          {linked}\n"
            f"  Run: supabase link --project-ref {expected}"
        )
    return expected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="db_target_guard",
        description="Name the Supabase target explicitly, or refuse.",
    )
    parser.add_argument(
        "--target",
        required=True,
        help="which declared [remotes.<name>] target this command is for",
    )
    # A NAMED option rather than a trailing positional, so argument order does
    # not matter. The pnpm wrappers set ``--run`` and the operator appends
    # ``--target x``; with a REMAINDER positional that trailing flag would be
    # swallowed as part of the command and silently never parsed — the guard
    # would then refuse every invocation for having no target, which reads as
    # the guard being broken rather than as the operator omitting something.
    parser.add_argument(
        "--run",
        default="",
        help="the supabase CLI command to run once the target is verified, e.g. 'db push'",
    )
    args = parser.parse_args(argv)

    try:
        config_text = CONFIG_TOML.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"db-target: could not read {CONFIG_TOML}: {exc}", file=sys.stderr)
        return 2

    try:
        ref = resolve(args.target, declared_targets(config_text), linked_ref(LINK_FILE))
    except GuardError as exc:
        print(f"db-target: {exc}", file=sys.stderr)
        return 2

    command = args.run.split()
    if not command:
        print(f"db-target: target {args.target!r} verified (project {ref}).")
        return 0

    # Resolve the binary rather than relying on PATH lookup inside the call,
    # matching ``preflight_migrations.py``. A missing CLI must refuse, not
    # raise — the operator gets an instruction instead of a traceback.
    binary = shutil.which("supabase")
    if binary is None:
        print(
            "db-target: the `supabase` CLI is not on PATH, so the verified target "
            "cannot be acted on. Install it: https://supabase.com/docs/guides/cli",
            file=sys.stderr,
        )
        return 2

    print(f"db-target: target {args.target!r} verified (project {ref}) — running: supabase {' '.join(command)}")
    return subprocess.call([binary, *command], cwd=REPO_ROOT)  # noqa: S603 — fixed argv, resolved binary


if __name__ == "__main__":
    raise SystemExit(main())
