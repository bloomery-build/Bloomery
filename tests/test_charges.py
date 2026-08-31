"""Tests for alloy/charge loading, dependency resolution, and installation."""

import json
import os

import pytest

from bloomery import alloys, charges
from bloomery.errors import (
    AlloyNotFoundError,
    ChargeBuildError,
    ChargeNotFoundError,
    CyclicChargeDependencyError,
)


def write(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


# ── alloy loading ─────────────────────────────────────────────────

def test_load_alloy_finds_a_bundled_alloy(tmp_path):
    alloy = alloys.load_alloy("pip", str(tmp_path))
    assert alloy["alloy"]["name"] == "pip"


def test_load_alloy_project_local_wins_over_bundled(tmp_path):
    write(tmp_path / "alloys" / "pip.toml", '[alloy]\nname = "pip"\nlanguage = "python"\n[commands]\ninstall = "custom"\n')
    alloy = alloys.load_alloy("pip", str(tmp_path))
    assert alloy["commands"]["install"] == "custom"


def test_load_alloy_missing_raises(tmp_path):
    with pytest.raises(AlloyNotFoundError, match="Alloy not found"):
        alloys.load_alloy("nosuchalloy", str(tmp_path))


def test_load_alloy_empty_name_returns_none(tmp_path):
    assert alloys.load_alloy("", str(tmp_path)) is None


# ── charge loading (charges live at <root>/<language>/<name>.toml,
#    addressed as "<language>/<name>") ─────────────────────────────

def test_load_charge_from_project_local_dir(tmp_path):
    write(tmp_path / "charges" / "c" / "zlib.toml", '[charge]\nname = "zlib"\nlanguage = "c"\nalloy = "source"\n')
    charge = charges.load_charge("c/zlib", str(tmp_path))
    assert charge["charge"]["name"] == "zlib"


def test_load_charge_missing_raises(tmp_path):
    with pytest.raises(ChargeNotFoundError, match="Charge not found"):
        charges.load_charge("c/nosuchcharge", str(tmp_path))


def test_load_charge_requires_language_prefix(tmp_path):
    with pytest.raises(ChargeNotFoundError, match="must be '<language>/<name>'"):
        charges.load_charge("zlib", str(tmp_path))


# ── dependency resolution ─────────────────────────────────────────

def test_resolve_dependency_order_puts_deps_first():
    charge_map = {
        "c/a": {"charge": {"depends": []}},
        "c/b": {"charge": {"depends": ["c/a"]}},
        "c/c": {"charge": {"depends": ["c/b"]}},
    }
    order = charges.resolve_dependency_order(charge_map)
    assert order.index("c/a") < order.index("c/b") < order.index("c/c")


def test_resolve_dependency_order_detects_cycles():
    charge_map = {
        "c/a": {"charge": {"depends": ["c/b"]}},
        "c/b": {"charge": {"depends": ["c/a"]}},
    }
    with pytest.raises(CyclicChargeDependencyError):
        charges.resolve_dependency_order(charge_map)


def test_resolve_dependency_order_unknown_dep_raises():
    charge_map = {"c/a": {"charge": {"depends": ["c/missing"]}}}
    with pytest.raises(ChargeNotFoundError, match="wasn't loaded"):
        charges.resolve_dependency_order(charge_map)


# ── installation ───────────────────────────────────────────────────

def test_install_charge_via_pip_alloy_runs_the_resolved_command(tmp_path, monkeypatch):
    ran = []

    class FakeResult:
        returncode = 0

    def fake_execute(command_line, project_dir):
        ran.append(command_line)
        return FakeResult()

    monkeypatch.setattr(charges.TaskRunner, "_execute", staticmethod(fake_execute))

    charge = {"charge": {"name": "requests", "language": "python", "alloy": "pip", "version": ">=2.31"}}
    pip_alloy = alloys.load_alloy("pip", str(tmp_path))

    dest = charges.install_charge(charge, pip_alloy, str(tmp_path), str(tmp_path / "vendor"))

    assert "pip install requests>=2.31" in ran[0]
    assert os.path.isdir(dest)


def test_install_charge_package_can_differ_from_name(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(charges.TaskRunner, "_execute", staticmethod(
        lambda cmd, d: ran.append(cmd) or type("R", (), {"returncode": 0})()))

    charge = {"charge": {"name": "pillow-fork", "language": "python", "alloy": "pip", "package": "Pillow"}}
    pip_alloy = alloys.load_alloy("pip", str(tmp_path))
    charges.install_charge(charge, pip_alloy, str(tmp_path), str(tmp_path / "vendor"))

    assert "pip install Pillow" in ran[0]


def test_install_charge_via_alloy_raises_on_nonzero_exit(tmp_path, monkeypatch):
    class FailResult:
        returncode = 1

    monkeypatch.setattr(charges.TaskRunner, "_execute", staticmethod(lambda *a: FailResult()))

    charge = {"charge": {"name": "requests", "language": "python", "alloy": "pip"}}
    pip_alloy = alloys.load_alloy("pip", str(tmp_path))

    with pytest.raises(ChargeBuildError, match="failed to install"):
        charges.install_charge(charge, pip_alloy, str(tmp_path), str(tmp_path / "vendor"))


def test_install_charge_source_alloy_clones_and_builds(tmp_path, monkeypatch):
    ran = []

    class OkResult:
        returncode = 0

    def fake_execute(command_line, project_dir):
        ran.append(command_line)
        return OkResult()

    monkeypatch.setattr(charges.TaskRunner, "_execute", staticmethod(fake_execute))

    charge = {
        "charge": {"name": "zlib", "language": "c", "alloy": "source"},
        "source": {"kind": "git", "url": "https://github.com/madler/zlib", "ref": "v1.3.1"},
        "build": {"command": "make", "flags": "-j4"},
    }
    source_alloy = alloys.load_alloy("source", str(tmp_path))

    dest = charges.install_charge(charge, source_alloy, str(tmp_path), str(tmp_path / "vendor"))

    assert any("git clone" in cmd for cmd in ran)
    assert any("make" in cmd for cmd in ran)
    assert os.path.isdir(dest)


def test_install_charges_writes_a_lockfile(tmp_path, monkeypatch):
    class OkResult:
        returncode = 0

    monkeypatch.setattr(charges.TaskRunner, "_execute", staticmethod(lambda *a: OkResult()))

    write(tmp_path / "charges" / "python" / "requests.toml",
          '[charge]\nname = "requests"\nlanguage = "python"\nalloy = "pip"\nversion = ">=2.31"\n')

    lock = charges.install_charges(["python/requests"], str(tmp_path))

    lock_path = tmp_path / ".bloomery" / "vendor" / "lock.json"
    assert lock_path.exists()
    data = json.loads(lock_path.read_text())
    assert data["python/requests"]["version"] == ">=2.31"
    assert lock == data


def test_ensure_installed_is_a_noop_without_auto_install(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(charges, "install_charges", lambda *a, **k: called.append(a))
    charges.ensure_installed({}, str(tmp_path))
    assert not called


def test_ensure_installed_runs_when_opted_in(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(charges, "install_charges", lambda items, *a, **k: called.append(items))
    config = {"charges": {"auto_install": True, "use": ["python/requests"]}}
    charges.ensure_installed(config, str(tmp_path))
    assert called == [["python/requests"]]


# ── ad-hoc bypass: no charge file needed ───────────────────────────

def test_parse_item_recognizes_charge_ref():
    assert charges._parse_item("python/requests") == ("ref", "python/requests")


def test_parse_item_recognizes_adhoc_with_version():
    assert charges._parse_item("python:flask@2.3.0") == ("adhoc", "python", "flask", "2.3.0")


def test_parse_item_recognizes_adhoc_without_version():
    assert charges._parse_item("python:flask") == ("adhoc", "python", "flask", "")


def test_install_charges_adhoc_item_needs_no_charge_file(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(charges.TaskRunner, "_execute", staticmethod(
        lambda cmd, d: ran.append(cmd) or type("R", (), {"returncode": 0})()))

    # no charges/ directory exists at all - purely ad hoc
    lock = charges.install_charges(["python:flask"], str(tmp_path))

    assert "pip install flask" in ran[0]
    assert lock["python:flask"]["package"] == "flask"
    assert lock["python:flask"]["alloy"] == "pip"


def test_install_charges_adhoc_item_pins_a_version(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(charges.TaskRunner, "_execute", staticmethod(
        lambda cmd, d: ran.append(cmd) or type("R", (), {"returncode": 0})()))

    charges.install_charges(["python:flask@2.3.0"], str(tmp_path))

    assert "pip install flask==2.3.0" in ran[0]


def test_install_charges_adhoc_defaults_to_the_languages_default_alloy(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(charges.TaskRunner, "_execute", staticmethod(
        lambda cmd, d: ran.append(cmd) or type("R", (), {"returncode": 0})()))

    charges.install_charges(["rust:serde@1.0"], str(tmp_path))

    assert "cargo add serde@1.0" in ran[0]


def test_install_charges_mixes_refs_and_adhoc_items(tmp_path, monkeypatch):
    class OkResult:
        returncode = 0

    monkeypatch.setattr(charges.TaskRunner, "_execute", staticmethod(lambda *a: OkResult()))
    write(tmp_path / "charges" / "python" / "requests.toml",
          '[charge]\nname = "requests"\nlanguage = "python"\nalloy = "pip"\nversion = ">=2.31"\n')

    lock = charges.install_charges(["python/requests", "python:flask"], str(tmp_path))

    assert set(lock) == {"python/requests", "python:flask"}
