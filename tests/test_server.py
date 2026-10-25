"""What a launch is given: the environment it sees and what its sandbox shows."""

import shutil

import pytest

from ouroboros_convy.server import CHECKOUT, Server


def test_a_launch_sees_ouroboros_settings_and_keys_but_not_convys_or_other_secrets(monkeypatch):
    monkeypatch.setenv("GW_API_KEY", "convy-gateway-key")
    monkeypatch.setenv("GITHUB_TOKEN", "unrelated")
    monkeypatch.setenv("GIGACHAT_CREDENTIALS", "giga")
    monkeypatch.setenv("OUROBOROS_MODEL", "gigachat::GigaChat-2-Max")
    environment = Server("min").environment()
    assert {"GIGACHAT_CREDENTIALS", "OUROBOROS_MODEL", "PATH"} <= environment.keys()
    assert not {"GW_API_KEY", "GITHUB_TOKEN"} & environment.keys()


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="no bubblewrap")
def test_the_sandbox_shows_a_providers_root_ca(tmp_path):
    bundle = tmp_path / "russian_trusted_root_ca.pem"
    jail = Server("min").jail(
        CHECKOUT / ".venv" / "bin" / "python", {"OUROBOROS_EXTRA_CA_BUNDLE": str(bundle)}
    )
    assert ["--ro-bind", str(bundle), str(bundle)] == jail[jail.index(str(bundle)) - 1 :][:3]
