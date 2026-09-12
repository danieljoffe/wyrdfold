"""The db-target guard must REFUSE on everything it cannot prove.

The guard's whole value is that a wrong answer is impossible, not unlikely —
so these tests are weighted towards the refusal paths. The one permissive case
(named target matches the link) is a single test; everything else asserts a
refusal, because a gate that fails open is worse than no gate at all (#1028).
"""

from __future__ import annotations

import pytest

from scripts.db_target_guard import (
    GuardError,
    declared_targets,
    linked_ref,
    resolve,
)

PROD = "swxiuutaikxbirauivjg"
STAGING = "stagingrefaaaaaaaa"

CONFIG = f"""
project_id = "wyrdfold"

[api]
port = 54321

[remotes.production]
project_id = "{PROD}"

[remotes.production.auth]
enabled = true

[remotes.staging]
project_id = "{STAGING}"
"""


# ---- parsing ---------------------------------------------------------------


def test_declares_both_targets() -> None:
    assert declared_targets(CONFIG) == {"production": PROD, "staging": STAGING}


def test_top_level_project_id_is_not_a_target() -> None:
    """``project_id = "wyrdfold"`` at the top of config.toml is the LOCAL stack
    name, not a remote. Treating it as a target would invent one that cannot be
    linked to, and its ref would never match anything."""
    assert "wyrdfold" not in declared_targets(CONFIG).values()


def test_nested_table_does_not_contribute_a_target() -> None:
    """``[remotes.production.auth]`` is configuration FOR a target, not another
    target. A parser that treated any ``remotes.*`` header as a target would
    offer ``production.auth`` as something you could push to."""
    assert set(declared_targets(CONFIG)) == {"production", "staging"}


def test_project_id_from_an_unrelated_section_is_ignored() -> None:
    """A loose parse that grabbed any ``project_id`` would silently adopt one
    from a section that has nothing to do with remotes — defeating the check
    while still appearing to work."""
    cfg = f'[db]\nproject_id = "notatarget"\n\n[remotes.production]\nproject_id = "{PROD}"\n'
    assert declared_targets(cfg) == {"production": PROD}


def test_duplicate_declaration_refuses_rather_than_picking() -> None:
    cfg = f'[remotes.production]\nproject_id = "{PROD}"\n\n[remotes.production]\nproject_id = "other"\n'
    with pytest.raises(GuardError, match="more than once"):
        declared_targets(cfg)


def test_comment_after_the_value_still_parses() -> None:
    cfg = f'[remotes.production]\nproject_id = "{PROD}"  # the live one\n'
    assert declared_targets(cfg) == {"production": PROD}


# ---- link file -------------------------------------------------------------


def test_missing_link_file_reads_as_unlinked(tmp_path) -> None:
    assert linked_ref(tmp_path / "nope") is None


def test_empty_link_file_reads_as_unlinked(tmp_path) -> None:
    p = tmp_path / "project-ref"
    p.write_text("   \n", encoding="utf-8")
    assert linked_ref(p) is None


def test_link_file_is_stripped(tmp_path) -> None:
    p = tmp_path / "project-ref"
    p.write_text(f"  {PROD}\n", encoding="utf-8")
    assert linked_ref(p) == PROD


# ---- resolution: the refusal battery ---------------------------------------


def test_matching_target_and_link_is_allowed() -> None:
    """The ONLY permissive path."""
    assert resolve("production", {"production": PROD}, PROD) == PROD


def test_target_that_is_not_declared_refuses() -> None:
    with pytest.raises(GuardError, match="unknown target"):
        resolve("prod", {"production": PROD}, PROD)


def test_no_declared_targets_refuses() -> None:
    with pytest.raises(GuardError, match="no \\[remotes"):
        resolve("production", {}, PROD)


def test_unlinked_refuses_and_names_the_expected_ref() -> None:
    """Refusing is not enough — it has to say what to do next, or the operator
    reaches for `supabase link` from memory and may link the wrong one."""
    with pytest.raises(GuardError) as exc:
        resolve("production", {"production": PROD}, None)
    assert "not linked" in str(exc.value)
    assert PROD in str(exc.value)


def test_mismatch_refuses_and_names_both_projects() -> None:
    """The dangerous case: asking for staging while linked to production. The
    message must name what you asked for AND what you are actually pointed at,
    because "wrong project" alone does not tell you which way round it is."""
    targets = {"production": PROD, "staging": STAGING}
    with pytest.raises(GuardError) as exc:
        resolve("staging", targets, PROD)
    msg = str(exc.value)
    assert "REFUSING" in msg
    assert STAGING in msg and PROD in msg
    assert "production" in msg  # names the project actually linked


def test_mismatch_against_an_undeclared_link_still_refuses() -> None:
    """Linked to something config.toml has never heard of. It cannot be named,
    but it must still refuse — an unrecognised target is not a safe one."""
    with pytest.raises(GuardError, match=r"not declared in config\.toml"):
        resolve("production", {"production": PROD}, "someotherproject")


# ---- main(): what actually gets executed ------------------------------------
#
# The refusal battery above proves the DECISION. These prove the CONSEQUENCE —
# that a refusal runs nothing at all, and that a verified target runs exactly
# the intended supabase command. Asserting the decision without asserting the
# consequence would leave the dangerous half untested.


def _wire(monkeypatch, tmp_path, *, config: str, linked: str | None):
    from scripts import db_target_guard as mod

    cfg = tmp_path / "config.toml"
    cfg.write_text(config, encoding="utf-8")
    link = tmp_path / "project-ref"
    if linked is not None:
        link.write_text(linked, encoding="utf-8")
    monkeypatch.setattr(mod, "CONFIG_TOML", cfg)
    monkeypatch.setattr(mod, "LINK_FILE", link)
    calls: list[list[str]] = []
    monkeypatch.setattr(mod, "subprocess", type("S", (), {"call": staticmethod(lambda c, **k: calls.append(c) or 0)}))
    # The guard resolves `supabase` via shutil.which so a missing CLI refuses
    # instead of raising. Pin it here: the test asserts the argv it builds, not
    # where this machine happens to keep the binary.
    monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/local/bin/supabase")
    return mod, calls


def test_verified_target_runs_exactly_the_named_command(monkeypatch, tmp_path) -> None:
    mod, calls = _wire(monkeypatch, tmp_path, config=CONFIG, linked=PROD)
    rc = mod.main(["--target", "production", "--run", "db push"])
    assert rc == 0
    assert calls == [["/usr/local/bin/supabase", "db", "push"]]


def test_refusal_runs_nothing(monkeypatch, tmp_path) -> None:
    """The load-bearing assertion. A guard that printed a refusal and still
    shelled out would be worse than no guard — it would look safe."""
    mod, calls = _wire(monkeypatch, tmp_path, config=CONFIG, linked=PROD)
    rc = mod.main(["--target", "staging", "--run", "db push"])
    assert rc == 2
    assert calls == []


def test_unlinked_runs_nothing(monkeypatch, tmp_path) -> None:
    mod, calls = _wire(monkeypatch, tmp_path, config=CONFIG, linked=None)
    rc = mod.main(["--target", "production", "--run", "db push"])
    assert rc == 2
    assert calls == []


def test_verify_only_runs_nothing_and_succeeds(monkeypatch, tmp_path) -> None:
    """`pnpm db:target --target production` is a check, not an action."""
    mod, calls = _wire(monkeypatch, tmp_path, config=CONFIG, linked=PROD)
    assert mod.main(["--target", "production"]) == 0
    assert calls == []


def test_missing_supabase_cli_refuses_instead_of_raising(monkeypatch, tmp_path) -> None:
    """A verified target with no CLI installed must still exit non-zero with an
    instruction. Letting FileNotFoundError escape would print a traceback and,
    worse, a non-2 exit code that callers may not treat as a refusal."""
    mod, calls = _wire(monkeypatch, tmp_path, config=CONFIG, linked=PROD)
    monkeypatch.setattr(mod.shutil, "which", lambda _name: None)
    assert mod.main(["--target", "production", "--run", "db push"]) == 2
    assert calls == []
