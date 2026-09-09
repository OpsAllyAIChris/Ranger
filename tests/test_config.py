from __future__ import annotations

import os
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
        path.read_text(encoding="utf-8").replace(
            'accounts = "Accounts"', 'accounts = "../Escaped"'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="outside the vault root"):
        load_config(path, load_env=False)


def test_readonly_folder_under_ranger_is_rejected(config_file):
    path = config_file()
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'knowledge = "Knowledge"', 'knowledge = "Ranger/Knowledge"'
        ),
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
# that is not a path. The loader normalises the text before parsing.
#
# These test the normaliser at the string level on purpose. Whether
# C:\Users\Chris is a valid *path* depends on the platform; whether the TOML
# reader mangles it does not. Keeping them string-level means they mean the
# same thing on Windows and on Linux.


def repair(root_line: str) -> tuple[str, list[str]]:
    from ranger.config import _repair_windows_paths

    return _repair_windows_paths(f"[vault]\n{root_line}\n")


def value_of(text: str) -> str:
    import tomllib

    return tomllib.loads(text)["vault"]["root"]


def test_backslash_path_survives_the_toml_reader():
    repaired, notes = repair(r'root = "C:\Users\Chris\Vault"')
    assert value_of(repaired) == r"C:\Users\Chris\Vault"
    assert notes and "single quotes" in notes[0]


def test_path_whose_escapes_are_all_valid_is_not_silently_mangled():
    r"""C:\temp\notes parses with no error at all into C:<tab>emp<newline>otes.

    This is the case no error message could ever catch, and the reason the
    repair runs before the parse rather than after it.
    """
    raw = r'root = "C:\temp\notes"'
    assert "\t" in value_of(f"[vault]\n{raw}\n")  # what it would have been

    repaired, notes = repair(raw)
    assert value_of(repaired) == r"C:\temp\notes"
    assert notes


def test_already_escaped_path_is_left_alone():
    """Whoever wrote \\\\ meant it. Do not double it again."""
    repaired, notes = repair(r'root = "C:\\Users\\Chris\\Vault"')
    assert value_of(repaired) == r"C:\Users\Chris\Vault"
    assert notes == []


def test_literal_string_needs_no_repair():
    repaired, notes = repair(r"root = 'C:\Users\Chris\Vault'")
    assert value_of(repaired) == r"C:\Users\Chris\Vault"
    assert notes == []


def test_path_containing_an_apostrophe_takes_the_escaping_branch():
    """A TOML literal string cannot hold an apostrophe."""
    repaired, notes = repair('root = "C:\\Users\\O\'Brien\\Vault"')
    assert value_of(repaired) == "C:\\Users\\O'Brien\\Vault"
    assert notes


def test_forward_slash_root_is_untouched():
    """The value shipped in the repo's ranger.toml."""
    raw = 'root = "~/Obsidian/Ranger-Vault"'
    repaired, notes = repair(raw)
    assert repaired == f"[vault]\n{raw}\n"
    assert notes == []


def test_repair_only_touches_the_vault_table():
    from ranger.config import _repair_windows_paths

    text = '[drafts]\nroot = "C:\\Users\\x"\n'
    repaired, notes = _repair_windows_paths(text)
    assert repaired == text and notes == []


def test_repair_preserves_a_trailing_newline():
    from ranger.config import _repair_windows_paths

    assert _repair_windows_paths("[vault]\n")[0] == "[vault]\n"
    assert _repair_windows_paths("[vault]")[0] == "[vault]"


def test_the_shipped_config_file_loads(tmp_path, monkeypatch):
    """ranger.toml as committed, with its ~ root, parses and validates."""
    monkeypatch.delenv("RANGER_VAULT_ROOT", raising=False)
    loaded = load_config(Path(__file__).resolve().parent.parent / "ranger.toml", load_env=False)
    assert loaded.vault.root == (Path.home() / "Obsidian" / "Ranger-Vault").resolve()
    assert all("backslash" not in w for w in loaded.warnings)


def test_broken_toml_raises_with_a_backslash_hint(config_file):
    path = config_file()
    path.write_text('[model]\nname = "x\\q"\n', encoding="utf-8")

    with pytest.raises(ConfigError) as caught:
        load_config(path, load_env=False)
    assert "not valid TOML" in str(caught.value)
    assert "backslash" in str(caught.value)


# --- the vault root must be absolute --------------------------------------


def test_relative_vault_root_is_rejected(tmp_path, config_file):
    """A relative root resolves against the working directory.

    That is how a stray 'C:\\tmp\\...' tree once got written into this
    repository: a Windows-shaped root is relative on POSIX, so mkdir built it
    under the current directory.
    """
    path = config_file()
    text = path.read_text(encoding="utf-8")
    path.write_text(
        re.sub(r"^root = .*$", lambda _: "root = 'Obsidian/Vault'", text, count=1, flags=re.M),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="absolute path"):
        load_config(path, load_env=False)


def test_windows_root_on_posix_is_rejected_by_name(tmp_path, config_file):
    path = config_file()
    text = path.read_text(encoding="utf-8")
    path.write_text(
        re.sub(r"^root = .*$", lambda _: r"root = 'C:\tmp\Vault'", text, count=1, flags=re.M),
        encoding="utf-8",
    )
    if os.name == "nt":
        load_config(path, load_env=False)  # absolute on Windows, nothing to reject
    else:
        with pytest.raises(ConfigError, match="not Windows"):
            load_config(path, load_env=False)


def test_relative_env_override_is_rejected_too(config_file, monkeypatch):
    monkeypatch.setenv("RANGER_VAULT_ROOT", "some/relative/vault")
    with pytest.raises(ConfigError, match="RANGER_VAULT_ROOT"):
        load_config(config_file(), load_env=False)


# --- ranger.local.toml ----------------------------------------------------
#
# ranger.toml is tracked and changes as Ranger is built, so the operator
# editing it means a merge conflict on every pull. Their settings go in a
# git-ignored file beside it that overrides key by key.


def local_beside(config_file, body: str):
    path = config_file()
    (path.parent / (path.stem + ".local.toml")).write_text(body, encoding="utf-8")
    return path


def test_the_local_file_overrides_one_key(config_file):
    path = local_beside(config_file, '[tts]\nvoice_id = "iP95p4xOKvK53GoZ742B"\n')
    loaded = load_config(path, load_env=False)

    assert loaded.tts.voice_id == "iP95p4xOKvK53GoZ742B"
    assert loaded.overrides == ("tts.voice_id",)
    assert loaded.local_path is not None


def test_overriding_one_key_keeps_the_rest_of_the_section(config_file):
    """Setting tts.voice_id must not discard tts.model_id."""
    path = local_beside(config_file, '[tts]\nvoice_id = "abc"\n')
    loaded = load_config(path, load_env=False)

    assert loaded.tts.voice_id == "abc"
    assert loaded.tts.model_id == "eleven_flash_v2_5"
    assert loaded.tts.output_format == "pcm_24000"


def test_several_sections_can_be_overridden(config_file):
    path = local_beside(
        config_file,
        '[tts]\nvoice_id = "abc"\n\n[voice]\ninput_device = "C920"\n\n[model]\neffort = "high"\n',
    )
    loaded = load_config(path, load_env=False)

    assert loaded.tts.voice_id == "abc"
    assert loaded.voice.input_device == "C920"
    assert loaded.model.effort == "high"
    assert set(loaded.overrides) == {"tts.voice_id", "voice.input_device", "model.effort"}


def test_no_local_file_is_the_normal_case(config_file):
    loaded = load_config(config_file(), load_env=False)
    assert loaded.local_path is None and loaded.overrides == ()


def test_a_typo_in_the_local_file_is_caught_too(config_file):
    path = local_beside(config_file, '[tts]\nvoyce_id = "abc"\n')
    with pytest.raises(ConfigError, match="tts.voyce_id"):
        load_config(path, load_env=False)


def test_a_setting_in_the_wrong_section_locally_still_points_at_the_right_one(config_file):
    path = local_beside(config_file, '[voice]\nvoice_id = "abc"\n')
    with pytest.raises(ConfigError, match="Did you mean tts.voice_id"):
        load_config(path, load_env=False)


def test_a_broken_local_file_names_itself(config_file):
    path = local_beside(config_file, "[tts\nvoice_id =\n")
    with pytest.raises(ConfigError, match=r"local\.toml is not valid TOML"):
        load_config(path, load_env=False)


def test_the_local_file_can_move_the_vault(config_file, tmp_path):
    """The whole point: the operator's own paths, kept out of the tracked file."""
    other = tmp_path / "Elsewhere" / "Vault"
    other.mkdir(parents=True)
    path = local_beside(config_file, f"[vault]\nroot = '{other.as_posix()}'\n")
    loaded = load_config(path, load_env=False)

    assert loaded.vault.root == other.resolve()
    assert loaded.vault.drafts == (other / "Ranger" / "drafts").resolve()


def test_an_env_override_still_beats_the_local_file(config_file, tmp_path, monkeypatch):
    """RANGER_VAULT_ROOT stays the last word, as it was before."""
    local_root = tmp_path / "FromLocal"
    env_root = tmp_path / "FromEnv"
    for folder in (local_root, env_root):
        folder.mkdir()
    path = local_beside(config_file, f"[vault]\nroot = '{local_root.as_posix()}'\n")
    monkeypatch.setenv("RANGER_VAULT_ROOT", str(env_root))

    assert load_config(path, load_env=False).vault.root == env_root.resolve()
