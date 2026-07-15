"""Tests for ``HermesCLI._inject_shell_output``.

When the agent is running, shell output should be injected via
``agent.steer()`` mid-turn rather than queued — this keeps the tool call
alive and lets the model see the new input as part of its next iteration.

Falling back to ``_pending_input.put()`` only when steer fails or isn't
available ensures nothing is silently dropped.  When idle, output goes
to the queue for the next turn like any other user message.

These tests exercise the full routing contract without starting a
prompt_toolkit app.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch


def _make_cli():
    """Create a HermesCLI instance with prompt_toolkit stubbed out."""
    _clean_config = {
        "model": {"default": "anthropic/claude-opus-4.6", "provider": "auto"},
        "display": {"compact": False, "tool_progress": "all"},
        "terminal": {"env_type": "local"},
    }
    clean_env = {"LLM_MODEL": "", "HERMES_MAX_ITERATIONS": ""}
    prompt_toolkit_stubs = {
        "prompt_toolkit",
        "prompt_toolkit.history",
        "prompt_toolkit.styles",
        "prompt_toolkit.patch_stdout",
        "prompt_toolkit.application",
        "prompt_toolkit.layout",
        "prompt_toolkit.layout.processors",
        "prompt_toolkit.filters",
        "prompt_toolkit.layout.dimension",
        "prompt_toolkit.layout.menus",
        "prompt_toolkit.widgets",
        "prompt_toolkit.key_binding",
        "prompt_toolkit.completion",
        "prompt_toolkit.formatted_text",
        "prompt_toolkit.auto_suggest",
    }
    with patch.dict(sys.modules, {n: MagicMock() for n in prompt_toolkit_stubs}), patch.dict(  # noqa: E402
        "os.environ", clean_env, clear=False
    ):
        import cli as _cli_mod

        return _cli_mod.HermesCLI()


class TestInjectShellOutput:
    """``_inject_shell_output`` routing contract."""

    def test_agent_running_steers_output(self):
        """When the agent is running and steer() is available, shell output
        should be injected mid-turn via agent.steer()."""
        cli = _make_cli()
        cli._agent_running = True
        cli.agent = MagicMock()
        cli.agent.steer = MagicMock(return_value=True)

        formatted = "Some shell output"
        cli._inject_shell_output(formatted)

        cli.agent.steer.assert_called_once_with("Some shell output")

    def test_agent_idle_queues_output(self):
        """When the agent is idle, shell output should be queued as next turn."""
        cli = _make_cli()
        cli._agent_running = False
        cli.agent = MagicMock()
        cli._pending_input = MagicMock()

        formatted = "Some shell output"
        cli._inject_shell_output(formatted)

        # No steer call expected when idle
        cli.agent.steer.assert_not_called()
        cli._pending_input.put.assert_called_once_with("Some shell output")

    def test_steer_failure_falls_back_to_queue(self):
        """When steer raises an exception, fall back to _pending_input."""
        cli = _make_cli()
        cli._agent_running = True
        cli.agent = MagicMock()
        cli.agent.steer = MagicMock(side_effect=RuntimeError("boom"))
        cli._pending_input = MagicMock()

        formatted = "Some shell output"
        cli._inject_shell_output(formatted)

        # steer was attempted, but the exception caused fallback
        cli.agent.steer.assert_called_once_with("Some shell output")
        cli._pending_input.put.assert_called_once_with("Some shell output")

    def test_steer_rejected_falls_back_to_queue(self):
        """When steer returns False (rejected), fall back to _pending_input."""
        cli = _make_cli()
        cli._agent_running = True
        cli.agent = MagicMock()
        cli.agent.steer = MagicMock(return_value=False)
        cli._pending_input = MagicMock()

        formatted = "Some shell output"
        cli._inject_shell_output(formatted)

        cli.agent.steer.assert_called_once_with("Some shell output")
        cli._pending_input.put.assert_called_once_with("Some shell output")

    def test_agent_lacks_steer_method_falls_back_to_queue(self):
        """When the agent has no steer() method, fall back to _pending_input."""
        # A simple object without a steer attribute — unlike MagicMock which
        # synthesises mocks for any attribute access.  This tests the real
        # hasattr check the production code uses.
        class BareAgent:
            """Minimal stand-in that has no steer method at all."""

        cli = _make_cli()
        cli._agent_running = True
        cli.agent = BareAgent()
        cli._pending_input = MagicMock()

        formatted = "Some shell output"
        cli._inject_shell_output(formatted)

        # hasattr check fails, so steer was never called
        cli._pending_input.put.assert_called_once_with("Some shell output")

    def test_agent_is_none_falls_back_to_queue(self):
        """When self.agent is None, fall back to _pending_input."""
        cli = _make_cli()
        cli._agent_running = True
        cli.agent = None  # type: ignore[assignment]
        cli._pending_input = MagicMock()

        formatted = "Some shell output"
        cli._inject_shell_output(formatted)

        # No steer call expected when agent is None
        cli._pending_input.put.assert_called_once_with("Some shell output")

