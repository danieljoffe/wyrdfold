"""One credential, one spelling.

The app reads ``OPENROUTER_API_KEY`` (``Settings.openrouter_api_key``); the eval
harness used to read ``OPEN_ROUTER_API_KEY``. The only live key lived in
``.env.local`` under the app's name while ~/.zshrc held a DEAD key under the
harness's name — so `source ~/.zshrc` actively SET the broken one and every eval
401'd. #780 sat blocked on that for a day.

Every test here is hermetic: the real ``.env.local`` and ~/.zshrc are pointed at
tmp fixtures, so the suite can neither read nor leak a real key.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import _openrouter


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPEN_ROUTER_API_KEY", raising=False)
    # Point both file sources at empty fixtures — never the developer's own.
    monkeypatch.setattr(_openrouter, "ENV_LOCAL", tmp_path / ".env.local")
    monkeypatch.setattr(_openrouter, "ZSHRC", tmp_path / ".zshrc")


def test_canonical_name_matches_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """If this drifts from Settings, harness and prod disagree again."""
    from app.config import Settings

    assert _openrouter._KEY_ENV == "OPENROUTER_API_KEY"
    assert "openrouter_api_key" in Settings.model_fields


def test_reads_the_canonical_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-canonical")
    assert _openrouter.get_api_key() == "sk-canonical"


def test_legacy_env_var_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """An old shell keeps working rather than failing confusingly."""
    monkeypatch.setenv("OPEN_ROUTER_API_KEY", "sk-legacy")
    assert _openrouter.get_api_key() == "sk-legacy"


def test_canonical_env_beats_stale_legacy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPEN_ROUTER_API_KEY", "sk-dead")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-live")
    assert _openrouter.get_api_key() == "sk-live"


def test_canonical_in_a_file_beats_stale_legacy_in_the_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """This is the exact shape of the original outage.

    A dead legacy key exported in the shell must NOT shadow the live canonical
    key sitting in .env.local. Per-source precedence would return "sk-dead".
    """
    (tmp_path / ".env.local").write_text("OPENROUTER_API_KEY=sk-live-from-file\n")
    monkeypatch.setenv("OPEN_ROUTER_API_KEY", "sk-dead-from-shell")
    assert _openrouter.get_api_key() == "sk-live-from-file"


def test_falls_back_to_env_local_with_no_env_vars(tmp_path: Path) -> None:
    (tmp_path / ".env.local").write_text("OPENROUTER_API_KEY=sk-from-file\n")
    assert _openrouter.get_api_key() == "sk-from-file"


def test_falls_back_to_zshrc_last(tmp_path: Path) -> None:
    (tmp_path / ".zshrc").write_text('export OPENROUTER_API_KEY="sk-from-zshrc"\n')
    assert _openrouter.get_api_key() == "sk-from-zshrc"


def test_error_names_the_canonical_var() -> None:
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        _openrouter.get_api_key()
