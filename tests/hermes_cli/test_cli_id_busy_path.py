"""Classic-CLI /id while the agent runs.

Typed mid-turn it used to land in ``_pending_input`` and only answered after the turn ended. It
now runs inline on the UI thread and paints the session ID in the /btw side-result panel from a
daemon thread, without queueing, interrupting or steering the foreground turn.
"""

import threading
from types import SimpleNamespace

import pytest


def _make_cli():
    from cli import HermesCLI

    return HermesCLI.__new__(HermesCLI)


@pytest.mark.parametrize("text", ["/id", "/ID", "  /id  "])
def test_id_dispatches_inline_while_running(text):
    cli = _make_cli()
    cli._agent_running = True
    assert cli._should_handle_readonly_command_inline(text.strip()) is True


def test_id_takes_the_normal_path_when_idle_or_with_images():
    cli = _make_cli()
    cli._agent_running = False
    assert cli._should_handle_readonly_command_inline("/id") is False
    cli._agent_running = True
    assert cli._should_handle_readonly_command_inline("/id", has_images=True) is False
    assert cli._should_handle_readonly_command_inline("/model foo") is False


def test_enter_runs_id_inline_without_touching_the_turn(monkeypatch):
    cli = _make_cli()
    cli._agent_running = True
    cli.session_id = "20261119_120000_abc123"
    painted = threading.Event()
    calls = []

    def fake_panel(_cli, *, header_lines, body, title_suffix, empty_note, console=None):
        calls.append((header_lines, body, title_suffix))
        painted.set()

    agent = SimpleNamespace(
        steer=lambda *_: pytest.fail("steered"), interrupt=lambda *_: pytest.fail("interrupted"),
        redirect=lambda *_: pytest.fail("redirected"))
    cli.agent = agent
    cli._pending_input = SimpleNamespace(put=lambda *_: pytest.fail("queued"))
    cli._app = SimpleNamespace(invalidate=lambda: None)
    cli._invalidate = lambda **_: None
    cli._should_handle_model_command_inline = lambda *a, **k: False
    buf = SimpleNamespace(reset=lambda **k: None)
    event = SimpleNamespace(app=SimpleNamespace(current_buffer=buf, invalidate=lambda: None))

    monkeypatch.setattr("hermes_cli.cli_commands_mixin._print_side_result_panel", fake_panel)
    assert cli._tui_enter_inline_command(event, "/id", False) is True
    assert painted.wait(5)
    assert calls == [(["  🆔 /id"], "20261119_120000_abc123", "(session id)")]
