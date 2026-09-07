from __future__ import annotations

import pytest

from ranger.vault import Vault, VaultError, VaultPathDenied, VaultWriteDenied


@pytest.fixture
def vault(config):
    return Vault(config.vault)


def test_reads_anywhere_inside_the_vault(vault, vault_root):
    note = vault_root / "Accounts" / "Illes Foods.md"
    note.write_text("Rod asked about the film program.", encoding="utf-8")
    assert "film program" in vault.read_text(note)


def test_read_outside_the_vault_is_denied(vault, tmp_path):
    outside = tmp_path / "secrets.md"
    outside.write_text("nope", encoding="utf-8")
    with pytest.raises(VaultPathDenied):
        vault.read_text(outside)


def test_traversal_out_of_the_vault_is_denied(vault):
    with pytest.raises(VaultPathDenied):
        vault.read_text("Accounts/../../escape.md")


def test_writes_into_ranger_folders_are_allowed(vault, config):
    path = vault.write_new(config.vault.drafts / "follow-up.md", "draft body")
    assert path.read_text(encoding="utf-8") == "draft body"


@pytest.mark.parametrize("folder", ["Accounts", "Knowledge", ""])
def test_writes_outside_ranger_folders_are_denied(vault, vault_root, folder):
    target = vault_root / folder / "new.md" if folder else vault_root / "new.md"
    with pytest.raises(VaultWriteDenied):
        vault.write_new(target, "should never land")
    assert not target.exists()


def test_write_new_refuses_to_clobber(vault, config):
    target = config.vault.drafts / "held.md"
    vault.write_new(target, "first")
    with pytest.raises(VaultWriteDenied, match="does not overwrite"):
        vault.write_new(target, "second")
    assert target.read_text(encoding="utf-8") == "first"


def test_overwrite_needs_the_explicit_flag(vault, config):
    target = config.vault.memory / "facts.md"
    vault.write_new(target, "one")
    with pytest.raises(VaultWriteDenied):
        vault.overwrite(target, "two")
    vault.overwrite(target, "two", allow_overwrite=True)
    assert target.read_text(encoding="utf-8") == "two"


def test_log_folder_is_append_only(vault, config):
    target = config.vault.log / "2026-09-07.md"
    vault.append(target, "one\n")
    vault.append(target, "two\n")
    assert target.read_text(encoding="utf-8") == "one\ntwo\n"
    with pytest.raises(VaultWriteDenied, match="append-only"):
        vault.write_new(target, "clobber")
    with pytest.raises(VaultWriteDenied, match="append-only"):
        vault.overwrite(target, "clobber", allow_overwrite=True)


def test_symlink_out_of_ranger_folder_is_denied(vault, config, vault_root):
    escape = config.vault.drafts / "escape"
    try:
        escape.symlink_to(vault_root / "Accounts")
    except (OSError, NotImplementedError) as exc:
        # Windows needs developer mode or admin rights to make a symlink.
        pytest.skip(f"symlinks not permitted here: {exc}")
    with pytest.raises(VaultWriteDenied):
        vault.write_new(escape / "sneaky.md", "should never land")


def test_vault_has_no_delete_or_rename(vault):
    surface = {name for name in dir(vault) if not name.startswith("_")}
    assert not surface & {"delete", "remove", "unlink", "rename", "move"}


def test_relative_paths_use_forward_slashes(vault, vault_root):
    """These strings reach the prompt and the operator. Keep them portable."""
    nested = vault_root / "Accounts" / "Food" / "Illes Foods.md"
    nested.parent.mkdir(parents=True)
    nested.write_text("notes", encoding="utf-8")

    listed = vault.list_markdown(vault_root / "Accounts")
    assert [f.relative for f in listed] == ["Accounts/Food/Illes Foods.md"]
    assert "\\" not in listed[0].relative


def test_the_ranger_root_itself_is_writable(vault, config):
    """Amendment D permits the whole Ranger/ tree, not only the four folders.

    The kill switch lives at Ranger/paused.md, where it is obvious rather than
    buried inside the inbox.
    """
    path = vault.write_new(config.vault.ranger / "paused.md", "paused: true")
    assert path.read_text(encoding="utf-8") == "paused: true"


def test_widening_to_the_ranger_root_did_not_open_the_rest_of_the_vault(vault, vault_root):
    for target in (
        vault_root / "Accounts" / "new.md",
        vault_root / "Knowledge" / "new.md",
        vault_root / "new.md",
        vault_root.parent / "escaped.md",
    ):
        with pytest.raises(VaultError):
            vault.write_new(target, "should never land")
        assert not target.exists()
