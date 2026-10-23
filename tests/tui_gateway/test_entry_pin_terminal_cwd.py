"""The stdio TUI gateway anchors TERMINAL_CWD to its launch folder.

Without it, a later lazy ``import gateway.run`` resolves the ``terminal.cwd: .`` placeholder to
``$HOME``, and project ``.mcp.json`` discovery silently reads ``~/.mcp.json`` instead of the
folder ``hermes --tui`` was started in.
"""

import os

from tui_gateway import entry


def test_pins_launch_folder_when_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    monkeypatch.setenv("HERMES_CWD", str(tmp_path))
    entry._pin_launch_terminal_cwd()
    assert os.environ["TERMINAL_CWD"] == str(tmp_path)


def test_replaces_placeholder(monkeypatch, tmp_path):
    monkeypatch.setenv("TERMINAL_CWD", ".")
    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    monkeypatch.setenv("HERMES_CWD", str(tmp_path))
    entry._pin_launch_terminal_cwd()
    assert os.environ["TERMINAL_CWD"] == str(tmp_path)


def test_keeps_explicit_cwd(monkeypatch, tmp_path):
    monkeypatch.setenv("TERMINAL_CWD", "/explicit/dir")
    monkeypatch.setenv("HERMES_CWD", str(tmp_path))
    entry._pin_launch_terminal_cwd()
    assert os.environ["TERMINAL_CWD"] == "/explicit/dir"


def test_skips_non_local_backend(monkeypatch, tmp_path):
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("HERMES_CWD", str(tmp_path))
    entry._pin_launch_terminal_cwd()
    assert "TERMINAL_CWD" not in os.environ


def test_gateway_run_import_no_longer_moves_project_mcp_root(monkeypatch, tmp_path):
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {"probe": {"command": "echo"}}}')
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    monkeypatch.setenv("HERMES_CWD", str(tmp_path))
    entry._pin_launch_terminal_cwd()
    import gateway.run  # noqa: F401  (its import-time placeholder resolution must not override)

    from tools.mcp_tool_config import _load_project_mcp_json, _workspace_folder

    assert _workspace_folder() == str(tmp_path)
    assert "probe" in _load_project_mcp_json()
