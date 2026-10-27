"""Classic-CLI mid-run /goal, /subgoal and /yolo dispatch.

Typed while the agent runs they used to land in ``_pending_input`` and only ran after the turn
(shown as a queued prompt). Their ``CommandDef`` entries declare ``busy_policy="dispatch"``, so the
UI thread runs them inline, like /steer, /bg and /btw.
"""

import pytest


def _make_cli():
    from cli import HermesCLI

    return HermesCLI.__new__(HermesCLI)


@pytest.mark.parametrize("text", [
    "/goal show", "/goal pause", "/goal status", "/goal resume", "/goal ship the cache",
    "/subgoal", "/subgoal add check it", "/subgoal remove 1", "/yolo", "/GOAL show",
])
def test_state_commands_dispatch_inline_while_running(text):
    cli = _make_cli()
    cli._agent_running = True
    assert cli._should_handle_session_state_command_inline(text) is True


def test_state_commands_take_the_normal_path_when_idle_or_with_images():
    cli = _make_cli()
    cli._agent_running = False
    assert cli._should_handle_session_state_command_inline("/goal show") is False
    cli._agent_running = True
    assert cli._should_handle_session_state_command_inline("/goal show", has_images=True) is False
    assert cli._should_handle_session_state_command_inline("/model foo") is False


def test_enter_runs_goal_inline_instead_of_queueing():
    from types import SimpleNamespace

    cli = _make_cli()
    cli._agent_running = True
    ran = []
    cli.process_command = lambda text: ran.append(text) or True
    cli._should_handle_model_command_inline = lambda *a, **k: False
    buf = SimpleNamespace(reset=lambda **k: None)
    event = SimpleNamespace(app=SimpleNamespace(current_buffer=buf, invalidate=lambda: None))
    assert cli._tui_enter_inline_command(event, "/goal show", False) is True
    assert ran == ["/goal show"]
