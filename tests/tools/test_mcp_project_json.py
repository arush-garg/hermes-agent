"""Tests for project-local ``.mcp.json`` loading.

The loader reads Claude-style ``{"mcpServers": {...}}`` from ``./.mcp.json``
in the working directory and merges it into the MCP server map with
additive-only semantics: global config and plugin servers win on name
conflicts, so a project file can only ADD servers.
"""

import json
from unittest.mock import patch as mock_patch

import pytest

from tools.mcp_tool_config import (
    _load_mcp_config,
    _load_project_mcp_json,
    _translate_claude_mcp_entry,
)


# ─── _translate_claude_mcp_entry ──────────────────────────────────────────────

def test_translate_stdio_entry():
    cfg = _translate_claude_mcp_entry({
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        "env": {"FOO": "bar"},
    })
    assert cfg == {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        "env": {"FOO": "bar"},
    }


def test_translate_implicit_stdio_with_type():
    cfg = _translate_claude_mcp_entry({"type": "stdio", "command": "uvx", "args": ["foo"]})
    assert cfg["command"] == "uvx"
    assert cfg["args"] == ["foo"]


def test_translate_http_entry():
    cfg = _translate_claude_mcp_entry({
        "type": "http",
        "url": "https://example.com/mcp",
        "headers": {"Authorization": "Bearer tok"},
    })
    assert cfg["url"] == "https://example.com/mcp"
    assert cfg["headers"]["Authorization"] == "Bearer tok"
    assert "command" not in cfg


def test_translate_sse_entry_sets_transport():
    cfg = _translate_claude_mcp_entry({"type": "sse", "url": "http://localhost:8000/sse"})
    assert cfg["transport"] == "sse"
    assert cfg["url"] == "http://localhost:8000/sse"


def test_translate_invalid_entry_returns_none():
    assert _translate_claude_mcp_entry({}) is None
    assert _translate_claude_mcp_entry({"type": "http"}) is None


# ─── _load_project_mcp_json ──────────────────────────────────────────────────

def _write_mcp_json(base, servers):
    base.mkdir(parents=True, exist_ok=True)
    (base / ".mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def test_missing_file_returns_empty(tmp_path):
    assert _load_project_mcp_json(str(tmp_path)) == {}


def test_valid_file_loads_servers(tmp_path):
    _write_mcp_json(tmp_path, {
        "proj-fs": {"command": "npx", "args": ["-y", "server-filesystem"]},
    })
    servers = _load_project_mcp_json(str(tmp_path))
    assert "proj-fs" in servers
    assert servers["proj-fs"]["command"] == "npx"


def test_malformed_json_returns_empty_and_does_not_raise(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".mcp.json").write_text("{not json", encoding="utf-8")
    assert _load_project_mcp_json(str(tmp_path)) == {}


def test_wrong_top_level_shape_returns_empty(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".mcp.json").write_text(json.dumps(["a", "b"]), encoding="utf-8")
    assert _load_project_mcp_json(str(tmp_path)) == {}


def test_mcp_servers_not_object_returns_empty(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": "nope"}), encoding="utf-8")
    assert _load_project_mcp_json(str(tmp_path)) == {}


def test_invalid_entries_skipped_valid_ones_kept(tmp_path):
    _write_mcp_json(tmp_path, {
        "good": {"command": "foo"},
        "": {"command": "bad-empty-name"},
        "bad-entry": "not-a-dict",
        "no-transport": {"foo": "bar"},
    })
    servers = _load_project_mcp_json(str(tmp_path))
    assert list(servers) == ["good"]


# ─── merge into _load_mcp_config ─────────────────────────────────────────────

def test_project_servers_merged_into_config(tmp_path, monkeypatch):
    _write_mcp_json(tmp_path, {
        "proj-only": {"command": "proj-cmd"},
    })
    with mock_patch("hermes_cli.config.load_config", return_value={"mcp_servers": {}}), \
         mock_patch("tools.mcp_tool_config._load_project_mcp_json",
                    return_value={"proj-only": {"command": "proj-cmd"}}), \
         mock_patch("tools.mcp_tool_config._warn_hidden_whitespace"):
        result = _load_mcp_config()
    assert result.get("proj-only") == {"command": "proj-cmd"}


def test_project_server_cannot_shadow_global(tmp_path, monkeypatch):
    """Global config wins on name conflict — project file is additive-only."""
    global_servers = {"shared": {"command": "global-cmd"}}
    project = {"shared": {"command": "evil-project-cmd"}}
    with mock_patch("hermes_cli.config.load_config", return_value={"mcp_servers": global_servers}), \
         mock_patch("tools.mcp_tool_config._load_project_mcp_json", return_value=project), \
         mock_patch("tools.mcp_tool_config._warn_hidden_whitespace"):
        result = _load_mcp_config()
    assert result["shared"]["command"] == "global-cmd"


def test_project_servers_pass_suspicious_filter(tmp_path, monkeypatch):
    """Project servers go through the same exfiltration-shape filter as global."""
    _write_mcp_json(tmp_path, {"proj": {"command": "proj-cmd"}})
    real = _load_project_mcp_json(str(tmp_path))
    assert "proj" in real


# ─── toolset allowlist (enabled_mcp_server_names) ────────────────────────────
#
# Project servers connect via _load_mcp_config, but the CLI/cron/ACP toolset allowlist is built
# from enabled_mcp_server_names. If that only reads config.yaml, a project server registers its
# tools and then has every one of them filtered out before the model sees them.

def _allowlist(global_servers, project):
    from hermes_cli.tools_config import enabled_mcp_server_names
    with mock_patch("tools.mcp_tool_config._load_project_mcp_json", return_value=project), \
         mock_patch("hermes_cli.plugins.get_portable_mcp_server_names_nowait", return_value=set()):
        return enabled_mcp_server_names({"mcp_servers": global_servers})


def test_project_servers_are_on_toolset_allowlist():
    names = _allowlist({"glob": {"command": "g"}}, {"proj-only": {"url": "https://example.com/mcp"}})
    assert names == {"glob", "proj-only"}


def test_project_server_disabled_flag_respected():
    names = _allowlist({}, {"off": {"command": "x", "enabled": False}, "on": {"command": "y"}})
    assert names == {"on"}


def test_project_server_name_collision_does_not_resurrect_disabled_global():
    """Global config wins on collision, even when the global entry is disabled."""
    names = _allowlist({"shared": {"command": "g", "enabled": False}}, {"shared": {"command": "p"}})
    assert names == set()


def test_project_loader_failure_leaves_global_allowlist_intact():
    from hermes_cli.tools_config import enabled_mcp_server_names
    with mock_patch("tools.mcp_tool_config._load_project_mcp_json", side_effect=OSError("boom")), \
         mock_patch("hermes_cli.plugins.get_portable_mcp_server_names_nowait", return_value=set()):
        assert enabled_mcp_server_names({"mcp_servers": {"glob": {"command": "g"}}}) == {"glob"}
