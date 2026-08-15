"""Tests for _wake_idle_from_pending_steer — the periodic-monitor idle wake.

A monitor tick calls agent.steer(), which stashes text in agent._pending_steer.
conversation_loop drains it pre-API-call — but only during an active turn. If
the session is idle at the prompt, the steer waits forever. The CLI's idle
patrol drains it into the input queue so it becomes a fresh user turn.
"""

import queue

from cli import HermesCLI


class _FakeAgent:
    """Minimal agent stub exposing just the steer drain contract."""

    def __init__(self, steer: str | None) -> None:
        self._pending_steer = steer

    def _drain_pending_steer(self):
        text = self._pending_steer
        self._pending_steer = None
        return text


def _make_cli(agent):
    cli = object.__new__(HermesCLI)
    cli.agent = agent
    cli._pending_input = queue.Queue()
    return cli


def test_moves_pending_steer_into_input_queue():
    cli = _make_cli(_FakeAgent(steer="[Monitor #1] tick"))

    cli._wake_idle_from_pending_steer()

    assert cli.agent._pending_steer is None
    assert cli._pending_input.get_nowait() == "[Monitor #1] tick"


def test_concatenated_steer_preserved():
    cli = _make_cli(_FakeAgent(steer="[Monitor #1] one\n[Monitor #1] two"))

    cli._wake_idle_from_pending_steer()

    assert cli._pending_input.get_nowait() == "[Monitor #1] one\n[Monitor #1] two"


def test_noop_when_no_pending_steer():
    cli = _make_cli(_FakeAgent(steer=None))

    cli._wake_idle_from_pending_steer()

    assert cli._pending_input.empty()


def test_noop_when_agent_unset():
    cli = _make_cli(agent=None)

    cli._wake_idle_from_pending_steer()

    assert cli._pending_input.empty()


def test_noop_when_drain_raises():
    class _BoomAgent:
        def _drain_pending_steer(self):
            raise RuntimeError("boom")

    cli = _make_cli(_BoomAgent())

    cli._wake_idle_from_pending_steer()

    assert cli._pending_input.empty()