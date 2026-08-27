"""Tests for install/update/uninstall/init dispatch and the init scaffold."""

import sys

import pytest

from bloomery import selfmanage


def test_repo_root_finds_this_checkout():
    import os
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert selfmanage.repo_root() == repo_root


def test_self_commands_dispatch_without_touching_argparse(monkeypatch, tmp_path):
    """'bloomery install' must not be parsed as a project path."""
    ran = []
    monkeypatch.setattr(selfmanage, "_run", lambda *cmd: ran.append(cmd))
    monkeypatch.setattr("builtins.input", lambda *_: "x")
    monkeypatch.chdir(tmp_path)
    for name in ("install", "update", "uninstall", "init"):
        ran.clear()
        monkeypatch.setattr(sys, "argv", ["bloomery", name])
        assert selfmanage.cli() == 0


def test_update_falls_back_to_pip_upgrade_without_a_checkout(monkeypatch):
    ran = []
    monkeypatch.setattr(selfmanage, "repo_root", lambda: None)
    monkeypatch.setattr(selfmanage, "_run", lambda *cmd: ran.append(cmd))
    monkeypatch.setattr(sys, "argv", ["bloomery", "update"])
    assert selfmanage.cli() == 0
    assert "pip" in ran[0]
    assert "--upgrade" in ran[0]
    assert "bloomery-build" in ran[0]


def test_init_uses_the_bundled_template_not_a_home_directory_path(tmp_path, monkeypatch):
    """Regression: init_command used to look under ~/.bloomery/storage,
    which is empty on a fresh install — it must read the template that
    ships inside the package instead."""
    monkeypatch.chdir(tmp_path)
    answers = iter(["myproj", "c++"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))

    selfmanage.init_command()

    written = (tmp_path / "bloomery.toml").read_text(encoding="utf-8")
    assert 'name = "myproj"' in written
    assert 'system = "c++"' in written


def test_init_without_a_system_omits_the_system_line(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    answers = iter(["myproj", ""])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))

    selfmanage.init_command()

    written = (tmp_path / "bloomery.toml").read_text(encoding="utf-8")
    assert "system" not in written


def test_init_scaffold_is_valid_toml(tmp_path, monkeypatch):
    """The scaffolded file must actually parse, not just look plausible."""
    from bloomery import parse_toml

    monkeypatch.chdir(tmp_path)
    answers = iter(["myproj", "python"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))

    selfmanage.init_command()
    config = parse_toml(str(tmp_path / "bloomery.toml"))
    assert config["meta"]["name"] == "myproj"
    assert config["meta"]["system"] == "python"
