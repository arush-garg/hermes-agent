"""Regression coverage for one-shot interrupted-session recovery notices."""

from threading import Event

from agent import conversation_loop
from tools import checkpoint_manager


class _InterruptAgent:
    def __init__(self, message="", hard=False):
        self._interrupt_message = message
        self._hard_interrupt_requested = Event()
        if hard:
            self._hard_interrupt_requested.set()


def test_cron_timeout_is_not_mislabeled_as_user_interrupt():
    agent = _InterruptAgent("Cron job timed out (inactivity)", hard=True)

    assert conversation_loop._interrupt_exit_reason(agent) == "cron_inactivity_timeout"


def test_unattributed_hard_interrupt_is_not_mislabeled_as_user_interrupt():
    assert conversation_loop._interrupt_exit_reason(_InterruptAgent(hard=True)) == "hard_interrupt"


def test_normal_interrupt_keeps_user_provenance():
    assert conversation_loop._interrupt_exit_reason(_InterruptAgent()) == "interrupted_by_user"


class _Agent:
    def __init__(self):
        self.lines = []

    def _vprint(self, text, *, force=False):
        self.lines.append((text, force))


def test_recent_interruption_warning_is_acknowledged_after_render(monkeypatch):
    marker = {
        "session_id": "session-new",
        "timestamp": 1_000.0,
        "iso_time": "1970-01-01T00:16:40+0000",
        "reason": "interrupted:watchdog",
        "last_action": "executing tool: terminal",
    }
    cleared = []
    agent = _Agent()

    monkeypatch.setattr(conversation_loop.time, "time", lambda: 1_001.0)
    monkeypatch.setattr(checkpoint_manager, "list_interrupted_markers", lambda: [marker])
    monkeypatch.setattr(
        checkpoint_manager,
        "clear_interrupted_marker",
        lambda session_id: cleared.append(session_id) or True,
    )

    conversation_loop._surface_recent_interruption_warnings(agent)

    assert cleared == ["session-new"]
    assert [line for line, _ in agent.lines] == [
        "⚠️  Previous long-running session was interrupted before completing its task.",
        "   • session session-new at 1970-01-01T00:16:40+0000 — "
        "interrupted:watchdog last_action=executing tool: terminal",
        "   Partial file mutations may have been left on disk. "
        "Check `hermes checkpoints` / `/rollback` before trusting state.",
    ]


def test_stale_interruption_marker_is_not_rendered_or_acknowledged(monkeypatch):
    marker = {"session_id": "session-old", "timestamp": 1.0}
    cleared = []
    agent = _Agent()

    monkeypatch.setattr(conversation_loop.time, "time", lambda: 1.0 + 86_400.0)
    monkeypatch.setattr(checkpoint_manager, "list_interrupted_markers", lambda: [marker])
    monkeypatch.setattr(
        checkpoint_manager,
        "clear_interrupted_marker",
        lambda session_id: cleared.append(session_id) or True,
    )

    conversation_loop._surface_recent_interruption_warnings(agent)

    assert agent.lines == []
    assert cleared == []
