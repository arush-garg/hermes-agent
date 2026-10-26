"""``hermes mcp`` CLI sees project-local ``.mcp.json`` servers (additive, config.yaml wins) and
refuses to write them through config.yaml."""

import argparse
import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: home)
    monkeypatch.setattr("hermes_cli.config.get_config_path", lambda: home / "config.yaml")
    monkeypatch.setattr("hermes_cli.config.get_env_path", lambda: home / ".env")
    return home


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A workspace dir with a .mcp.json, made the cwd (runtime falls back to os.getcwd())."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "expo": {"type": "http", "url": "https://mcp.expo.dev/mcp", "auth": "oauth",
                 "oauth": {"flow": "browser"}},
        "shared": {"type": "http", "url": "https://project.example/mcp"},
    }}), encoding="utf-8")
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    monkeypatch.setattr("tools.file_tools_paths._authoritative_workspace_root", lambda *a, **k: None)
    monkeypatch.chdir(root)
    return root


def _seed_config(home: Path, mcp_servers: dict):
    import hermes_yaml as yaml

    with open(home / "config.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump({"mcp_servers": mcp_servers, "_config_version": 9}, f)


def _args(**kw):
    base = {"name": "expo", "url": None, "mcp_command": None, "args": None, "auth": None,
            "preset": None, "env": None, "flow": None, "all": False}
    base.update(kw)
    return argparse.Namespace(**base)


def _config_servers():
    from hermes_cli.config import load_config
    return load_config().get("mcp_servers") or {}


def test_login_finds_project_server(project, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr("hermes_cli.mcp_config._reauth_oauth_server",
                        lambda name, cfg, flow=None: calls.append((name, cfg)) or True)
    from hermes_cli.mcp_config import cmd_mcp_login

    cmd_mcp_login(_args(name="expo"))

    assert "not found" not in capsys.readouterr().out
    assert calls and calls[0][0] == "expo"
    assert calls[0][1]["url"] == "https://mcp.expo.dev/mcp"
    assert calls[0][1]["auth"] == "oauth"
    assert "type" not in calls[0][1]  # translated like the runtime does


def test_terminal_cwd_resolves_workspace(project, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr("tools.file_tools_paths._authoritative_workspace_root",
                        lambda *a, **k: str(project))
    from hermes_cli.mcp_config import _get_all_mcp_servers

    assert "expo" in _get_all_mcp_servers()


def test_cwd_project_found_despite_inherited_terminal_cwd(project, tmp_path, monkeypatch):
    """`cd proj && hermes mcp login expo` inside a Hermes terminal whose TERMINAL_CWD is a parent dir."""
    parent = tmp_path / "parent"
    parent.mkdir()
    monkeypatch.setattr("tools.file_tools_paths._authoritative_workspace_root",
                        lambda *a, **k: str(parent))
    from hermes_cli.mcp_config import _get_mcp_servers_with_origins

    servers, origins = _get_mcp_servers_with_origins()
    assert origins["expo"][0] == "project"


def test_config_yaml_wins_on_name_conflict(project, _isolate_config):
    _seed_config(_isolate_config, {"shared": {"url": "https://global.example/mcp"}})
    from hermes_cli.mcp_config import _get_mcp_servers_with_origins

    servers, origins = _get_mcp_servers_with_origins()
    assert servers["shared"]["url"] == "https://global.example/mcp"
    assert origins["shared"][0] == "config"
    assert origins["expo"][0] == "project"


def test_remove_refuses_project_server(project, _isolate_config, monkeypatch, capsys):
    _seed_config(_isolate_config, {"keep": {"url": "https://keep.example/mcp"}})
    monkeypatch.setattr("builtins.input", lambda _: "y")
    from hermes_cli.mcp_config import cmd_mcp_remove

    rc = cmd_mcp_remove(_args(name="expo"))

    assert rc == 1
    assert "comes from the workspace .mcp.json; edit that file instead" in capsys.readouterr().out
    assert set(_config_servers()) == {"keep"}
    assert "expo" in json.loads((project / ".mcp.json").read_text())["mcpServers"]


def test_add_refuses_project_server_name(project, monkeypatch, capsys):
    monkeypatch.setattr("hermes_cli.mcp_config._probe_single_server",
                        lambda *a, **k: pytest.fail("must not probe"))
    from hermes_cli.mcp_config import cmd_mcp_add

    rc = cmd_mcp_add(_args(name="expo", url="https://evil.example/mcp"))

    assert rc == 1
    assert "edit that file instead" in capsys.readouterr().out
    assert "expo" not in _config_servers()


def test_configure_refuses_project_server(project, capsys):
    from hermes_cli.mcp_config import cmd_mcp_configure

    assert cmd_mcp_configure(_args(name="expo")) == 1
    assert "edit that file instead" in capsys.readouterr().out


def test_list_shows_origin(project, _isolate_config, capsys):
    _seed_config(_isolate_config, {"shared": {"url": "https://global.example/mcp"}})
    from hermes_cli.mcp_config import cmd_mcp_list

    cmd_mcp_list()

    rows = {line.split()[0]: line for line in capsys.readouterr().out.splitlines()
            if line.strip() and line.split()[0] in {"expo", "shared"}}
    assert "(project)" in rows["expo"]
    assert "config.yaml" in rows["shared"] and "(project)" not in rows["shared"]
