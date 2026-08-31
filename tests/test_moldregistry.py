"""Tests for `bloomery mold list/search/get/init`, mocking the registry HTTP calls."""

import sys

import pytest

from bloomery import moldregistry, selfmanage
from bloomery.errors import BloomeryError, MoldDownloadError

INDEX = {
    "molds": [
        {"name": "python", "file": "python.toml", "description": "Default Python tooling mold"},
        {"name": "rust", "file": "rust.toml", "description": "Default Rust (cargo) mold"},
    ]
}


@pytest.fixture
def fake_client(monkeypatch):
    class FakeClient:
        def fetch_index(self):
            return INDEX

        def fetch_file(self, filename):
            return b'[bloomery]\nmold = "x"\n'

    client = FakeClient()
    monkeypatch.setattr(moldregistry, "_client", client)
    return client


def test_list_command_prints_every_mold(fake_client, capsys):
    moldregistry.list_command()
    out = capsys.readouterr().out
    assert "python" in out
    assert "rust" in out


def test_search_command_matches_name_or_description(fake_client, capsys):
    moldregistry.search_command("cargo")
    out = capsys.readouterr().out
    assert "rust" in out
    assert "python" not in out


def test_search_command_no_matches(fake_client, capsys):
    moldregistry.search_command("nope")
    out = capsys.readouterr().out
    assert "No molds match" in out


def test_get_command_writes_to_local_mold_dir(fake_client, tmp_path, monkeypatch):
    monkeypatch.setattr(moldregistry, "LOCAL_MOLD_DIR", str(tmp_path))
    moldregistry.get_command("python")
    written = (tmp_path / "python.toml").read_text(encoding="utf-8")
    assert 'mold = "x"' in written


def test_get_command_unknown_mold_raises(fake_client, tmp_path, monkeypatch):
    monkeypatch.setattr(moldregistry, "LOCAL_MOLD_DIR", str(tmp_path))
    with pytest.raises(MoldDownloadError, match="Mold not found"):
        moldregistry.get_command("nosuchmold")


def test_get_command_prompts_before_overwrite(fake_client, tmp_path, monkeypatch):
    monkeypatch.setattr(moldregistry, "LOCAL_MOLD_DIR", str(tmp_path))
    (tmp_path / "python.toml").write_text("stale", encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    moldregistry.get_command("python")
    assert (tmp_path / "python.toml").read_text(encoding="utf-8") == "stale"


def test_get_command_force_skips_prompt(fake_client, tmp_path, monkeypatch):
    monkeypatch.setattr(moldregistry, "LOCAL_MOLD_DIR", str(tmp_path))
    (tmp_path / "python.toml").write_text("stale", encoding="utf-8")

    def fail_if_called(*_):
        raise AssertionError("should not prompt with --force")

    monkeypatch.setattr("builtins.input", fail_if_called)
    moldregistry.get_command("python", force=True)
    assert 'mold = "x"' in (tmp_path / "python.toml").read_text(encoding="utf-8")


def test_init_command_scaffolds_a_local_mold(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    moldregistry.init_command("zig")
    written = (tmp_path / "zig.toml").read_text(encoding="utf-8")
    assert 'mold = "zig"' in written


def test_mold_group_rejects_unknown_subcommand():
    with pytest.raises(BloomeryError, match="Unknown mold subcommand"):
        moldregistry.mold_group(["bogus"])


def test_mold_group_requires_a_subcommand():
    with pytest.raises(BloomeryError, match="Usage"):
        moldregistry.mold_group([])


def test_cli_dispatches_mold_before_argparse(fake_client, monkeypatch, capsys):
    """'bloomery mold list' must not be parsed as a project path/target."""
    monkeypatch.setattr(sys, "argv", ["bloomery", "mold", "list"])
    assert selfmanage.cli() == 0
    out = capsys.readouterr().out
    assert "python" in out
