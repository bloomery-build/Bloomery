"""Tests for `bloomery charge list/search/get/install` and `bloomery alloy list/get`."""

import sys

import pytest

from bloomery import alloyregistry, chargeregistry, selfmanage
from bloomery.errors import BloomeryError, RegistryError

CHARGE_INDEX = {
    "charges": [
        {"name": "requests", "file": "python/requests.toml", "language": "python",
         "description": "HTTP library"},
        {"name": "zlib", "file": "c/zlib.toml", "language": "c",
         "description": "Compression library"},
    ]
}

ALLOY_INDEX = {
    "alloys": [
        {"name": "poetry", "file": "poetry.toml", "language": "python",
         "description": "Routes through poetry"},
    ]
}


@pytest.fixture
def fake_charge_client(monkeypatch):
    class FakeClient:
        def fetch_index(self):
            return CHARGE_INDEX

        def fetch_file(self, filename):
            return b'[charge]\nname = "x"\nlanguage = "python"\nalloy = "pip"\n'

    client = FakeClient()
    monkeypatch.setattr(chargeregistry, "_client", client)
    return client


@pytest.fixture
def fake_alloy_client(monkeypatch):
    class FakeClient:
        def fetch_index(self):
            return ALLOY_INDEX

        def fetch_file(self, filename):
            return b'[alloy]\nname = "poetry"\nlanguage = "python"\n[commands]\ninstall = "poetry add {charge.package}"\n'

    client = FakeClient()
    monkeypatch.setattr(alloyregistry, "_client", client)
    return client


# ── charge registry ──────────────────────────────────────────────

def test_charge_list_command(fake_charge_client, capsys):
    chargeregistry.list_command()
    out = capsys.readouterr().out
    assert "python/requests" in out
    assert "c/zlib" in out


def test_charge_search_command(fake_charge_client, capsys):
    chargeregistry.search_command("http")
    out = capsys.readouterr().out
    assert "python/requests" in out
    assert "c/zlib" not in out


def test_charge_get_command_writes_into_a_language_subfolder(fake_charge_client, tmp_path, monkeypatch):
    monkeypatch.setattr(chargeregistry, "LOCAL_CHARGE_DIR", str(tmp_path))
    chargeregistry.get_command("python/requests")
    written = (tmp_path / "python" / "requests.toml").read_text(encoding="utf-8")
    assert 'name = "x"' in written


def test_charge_get_command_unknown_raises(fake_charge_client, tmp_path, monkeypatch):
    monkeypatch.setattr(chargeregistry, "LOCAL_CHARGE_DIR", str(tmp_path))
    with pytest.raises(RegistryError, match="Charge not found"):
        chargeregistry.get_command("python/nosuch")


def test_charge_get_command_requires_language_prefix(fake_charge_client, tmp_path, monkeypatch):
    monkeypatch.setattr(chargeregistry, "LOCAL_CHARGE_DIR", str(tmp_path))
    with pytest.raises(BloomeryError, match="must be '<language>/<name>'"):
        chargeregistry.get_command("requests")


def test_charge_install_command_downloads_then_installs(fake_charge_client, tmp_path, monkeypatch):
    monkeypatch.setattr(chargeregistry, "LOCAL_CHARGE_DIR", str(tmp_path / "charges"))
    monkeypatch.chdir(tmp_path)

    class OkResult:
        returncode = 0

    import bloomery.charges as charges
    monkeypatch.setattr(charges.TaskRunner, "_execute", staticmethod(lambda *a: OkResult()))
    monkeypatch.setattr(charges, "charge_search_path",
                         lambda project_dir, config=None: iter([str(tmp_path / "charges")]))

    chargeregistry.install_command(["python/requests"])

    lock_path = tmp_path / ".bloomery" / "vendor" / "lock.json"
    assert lock_path.exists()


def test_charge_install_command_adhoc_item_skips_download(fake_charge_client, tmp_path, monkeypatch):
    """An ad-hoc "language:package" item bypasses charge lookup and
    download entirely - it must never hit get_command/the registry."""
    monkeypatch.chdir(tmp_path)

    def fail_if_called(*_a, **_k):
        raise AssertionError("ad-hoc items must not be downloaded as charges")

    monkeypatch.setattr(chargeregistry, "get_command", fail_if_called)

    class OkResult:
        returncode = 0

    import bloomery.charges as charges
    monkeypatch.setattr(charges.TaskRunner, "_execute", staticmethod(lambda *a: OkResult()))

    chargeregistry.install_command(["python:flask"])

    lock_path = tmp_path / ".bloomery" / "vendor" / "lock.json"
    assert lock_path.exists()


def test_charge_group_requires_a_subcommand():
    with pytest.raises(BloomeryError, match="Usage"):
        chargeregistry.charge_group([])


# ── alloy registry ───────────────────────────────────────────────

def test_alloy_list_command(fake_alloy_client, capsys):
    alloyregistry.list_command()
    out = capsys.readouterr().out
    assert "poetry" in out


def test_alloy_get_command_writes_to_local_dir(fake_alloy_client, tmp_path, monkeypatch):
    monkeypatch.setattr(alloyregistry, "LOCAL_ALLOY_DIR", str(tmp_path))
    alloyregistry.get_command("poetry")
    written = (tmp_path / "poetry.toml").read_text(encoding="utf-8")
    assert 'name = "poetry"' in written


def test_alloy_get_command_unknown_raises(fake_alloy_client, tmp_path, monkeypatch):
    monkeypatch.setattr(alloyregistry, "LOCAL_ALLOY_DIR", str(tmp_path))
    with pytest.raises(RegistryError, match="Alloy not found"):
        alloyregistry.get_command("nosuch")


def test_alloy_group_requires_a_subcommand():
    with pytest.raises(BloomeryError, match="Usage"):
        alloyregistry.alloy_group([])


# ── CLI dispatch ─────────────────────────────────────────────────

def test_cli_dispatches_charge_before_argparse(fake_charge_client, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["bloomery", "charge", "list"])
    assert selfmanage.cli() == 0
    assert "python/requests" in capsys.readouterr().out


def test_cli_dispatches_alloy_before_argparse(fake_alloy_client, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["bloomery", "alloy", "list"])
    assert selfmanage.cli() == 0
    assert "poetry" in capsys.readouterr().out
