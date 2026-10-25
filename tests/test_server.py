"""What a launch is given: the environment it sees."""

from ouroboros_convy.server import Server


def test_a_launch_sees_ouroboros_settings_and_keys_but_not_convys_or_other_secrets(monkeypatch):
    monkeypatch.setenv("GW_API_KEY", "convy-gateway-key")
    monkeypatch.setenv("GITHUB_TOKEN", "unrelated")
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "proxy-key")
    monkeypatch.setenv("OUROBOROS_MODEL", "openai-compatible::GigaChat-2-Max")
    environment = Server("min").environment()
    assert {"OPENAI_COMPATIBLE_API_KEY", "OUROBOROS_MODEL", "PATH"} <= environment.keys()
    assert not {"GW_API_KEY", "GITHUB_TOKEN"} & environment.keys()
