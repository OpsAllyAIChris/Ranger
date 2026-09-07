from __future__ import annotations

import re
from pathlib import Path

import pytest

from ranger.config import ConfigError, load_config


def test_loads_and_resolves_paths(config, vault_root):
    assert config.model.name == "test-model"
    assert config.vault.root == vault_root.resolve()
    assert config.vault.drafts == (vault_root / "Ranger" / "drafts").resolve()
    assert config.vault.append_only_roots == (config.vault.log,)


def test_morning_hour_inside_quiet_window_is_rejected(config_file):
    path = config_file(morning_hour=22, quiet_start=18, quiet_end=6)
    with pytest.raises(ConfigError, match="quiet window"):
        load_config(path, load_env=False)


def test_empty_quiet_window_is_rejected(config_file):
    path = config_file(quiet_start=6, quiet_end=6)
    with pytest.raises(ConfigError, match="no quiet window"):
        load_config(path, load_env=False)


def test_quiet_window_wraps_midnight(config):
    schedule = config.schedule
    assert schedule.in_quiet_hours(23)
    assert schedule.in_quiet_hours(2)
    assert schedule.in_quiet_hours(18)
    assert not schedule.in_quiet_hours(7)
    assert not schedule.in_quiet_hours(17)


def test_vault_subpath_escaping_root_is_rejected(config_file):
    path = config_file()
    path.write_text(
        path.read_text().replace('accounts = "Accounts"', 'accounts = "../Escaped"'),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="outside the vault root"):
        load_config(path, load_env=False)


def test_readonly_folder_under_ranger_is_rejected(config_file):
    path = config_file()
    path.write_text(
        path.read_text().replace('knowledge = "Knowledge"', 'knowledge = "Ranger/Knowledge"'),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="read only"):
        load_config(path, load_env=False)


def test_env_override_for_vault_root(tmp_path, config_file, monkeypatch):
    other = tmp_path / "External" / "Vault"
    other.mkdir(parents=True)
    monkeypatch.setenv("RANGER_VAULT_ROOT", str(other))
    loaded = load_config(config_file(), load_env=False)
    assert loaded.vault.root == other.resolve()


# --- Windows paths in TOML ------------------------------------------------
#
# A backslash inside a double-quoted TOML string is an escape sequence, so a
# pasted Windows path either fails to parse or, worse, parses into something
# that is not a path. The loader normalises before parsing.


def _write_root(tmp_path, config_file, raw_root_line):
    path = config_file()
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"^root = .*$", lambda _: raw_root_line, text, count=1, flags=re.M)
    path.write_text(text, encoding="utf-8")
    return path


def test_windows_path_with_backslashes_loads(tmp_path, config_file):
    path = _write_root(tmp_path, config_file, r'root = "C:\Users\Chris\Vault"')
    loaded = load_config(path, load_env=False)

    assert loaded.vault.root == Path(r"C:\Users\Chris\Vault").expanduser().resolve()
    assert any("backslash" in w or "Windows path" in w for w in loaded.warnings)


def test_windows_path_whose_escapes_are_all_valid_is_not_mangled(tmp_path, config_file):
    r"""C:\temp\notes parses with no error at all into C:<tab>emp<newline>otes.

    This is the case a better error message could never catch.
    """
    path = _write_root(tmp_path, config_file, r'root = "C:\temp\notes"')
    loaded = load_config(path, load_env=False)

    # Asserted on the string, not on Path.name: backslash is a separator on
    # Windows and an ordinary character on POSIX, and this test has to mean the
    # same thing on both. The corruption being guarded against is at the string
    # level anyway.
    text = str(loaded.vault.root)
    assert "\t" not in text and "\n" not in text
    assert text.endswith(r"C:\temp\notes")


def test_already_escaped_windows_path_is_left_alone(tmp_path, config_file):
    """Whoever wrote \\\\ meant it. Do not double it again."""
    path = _write_root(tmp_path, config_file, r'root = "C:\\Users\\Chris\\Vault"')
    loaded = load_config(path, load_env=False)

    assert loaded.vault.root == Path(r"C:\Users\Chris\Vault").expanduser().resolve()
    assert not loaded.warnings or all("backslash" not in w for w in loaded.warnings)


def test_literal_string_windows_path_needs_no_repair(tmp_path, config_file):
    path = _write_root(tmp_path, config_file, r"root = 'C:\Users\Chris\Vault'")
    loaded = load_config(path, load_env=False)

    assert loaded.vault.root == Path(r"C:\Users\Chris\Vault").expanduser().resolve()
    assert all("backslash" not in w for w in loaded.warnings)


def test_windows_path_containing_an_apostrophe(tmp_path, config_file):
    """A literal string cannot hold an apostrophe, so that branch escapes instead."""
    path = _write_root(tmp_path, config_file, r'root = "C:\Users\O\'Brien\Vault"'.replace("\\'", "'"))
    loaded = load_config(path, load_env=False)

    assert "O'Brien" in str(loaded.vault.root)
    assert "\t" not in str(loaded.vault.root)


def test_the_shipped_forward_slash_root_is_untouched(tmp_path, config_file):
    """The value in the repo's ranger.toml. No backslashes, so nothing to repair."""
    path = _write_root(tmp_path, config_file, 'root = "~/Obsidian/Ranger-Vault"')
    loaded = load_config(path, load_env=False)

    assert loaded.vault.root == (Path.home() / "Obsidian" / "Ranger-Vault").resolve()
    assert all("backslash" not in w for w in loaded.warnings)


def test_genuinely_broken_toml_still_raises_with_the_backslash_hint(tmp_path, config_file):
    path = config_file()
    path.write_text('[model]\nname = "x\\q"\n', encoding="utf-8")

    with pytest.raises(ConfigError) as caught:
        load_config(path, load_env=False)
    assert "not valid TOML" in str(caught.value)
    assert "backslash" in str(caught.value)


def test_repair_only_touches_the_vault_table(tmp_path, config_file):
    """A backslash in some other table is none of this function's business."""
    from ranger.config import _repair_windows_paths

    text = '[drafts]\nroot = "C:\\Users\\x"\n'
    repaired, notes = _repair_windows_paths(text)
    assert repaired == text
    assert notes == []
