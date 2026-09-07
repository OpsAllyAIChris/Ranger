from __future__ import annotations

import pytest

from ranger.config import ConfigError, load_config
from tests.conftest import write_config


def test_loads_and_resolves_paths(config, vault_root):
    assert config.model.name == "test-model"
    assert config.vault.root == vault_root.resolve()
    assert config.vault.drafts == (vault_root / "Ranger" / "drafts").resolve()
    assert config.vault.append_only_roots == (config.vault.log,)


def test_morning_hour_inside_quiet_window_is_rejected(tmp_path, vault_root):
    path = write_config(tmp_path, vault_root, morning_hour=22, quiet_start=18, quiet_end=6)
    with pytest.raises(ConfigError, match="quiet window"):
        load_config(path, load_env=False)


def test_empty_quiet_window_is_rejected(tmp_path, vault_root):
    path = write_config(tmp_path, vault_root, quiet_start=6, quiet_end=6)
    with pytest.raises(ConfigError, match="no quiet window"):
        load_config(path, load_env=False)


def test_quiet_window_wraps_midnight(config):
    schedule = config.schedule
    assert schedule.in_quiet_hours(23)
    assert schedule.in_quiet_hours(2)
    assert schedule.in_quiet_hours(18)
    assert not schedule.in_quiet_hours(7)
    assert not schedule.in_quiet_hours(17)


def test_vault_subpath_escaping_root_is_rejected(tmp_path, vault_root):
    path = write_config(tmp_path, vault_root)
    path.write_text(
        path.read_text().replace('accounts = "Accounts"', 'accounts = "../Escaped"'),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="outside the vault root"):
        load_config(path, load_env=False)


def test_readonly_folder_under_ranger_is_rejected(tmp_path, vault_root):
    path = write_config(tmp_path, vault_root)
    path.write_text(
        path.read_text().replace('knowledge = "Knowledge"', 'knowledge = "Ranger/Knowledge"'),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="read only"):
        load_config(path, load_env=False)


def test_env_override_for_vault_root(tmp_path, vault_root, monkeypatch):
    other = tmp_path / "External" / "Vault"
    other.mkdir(parents=True)
    monkeypatch.setenv("RANGER_VAULT_ROOT", str(other))
    loaded = load_config(write_config(tmp_path, vault_root), load_env=False)
    assert loaded.vault.root == other.resolve()
