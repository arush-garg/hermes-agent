"""Connectivity-loss pause: eligibility, pause loop, resume, interrupts, cap.

``agent/connectivity_probe.py`` is owned by a sibling builder and may not exist
yet, so these tests inject a stub module into ``sys.modules`` (the production
code late-imports it inside functions — patch where production reads).
Wall-clock is faked by swapping the ``time`` reference the module under test
reads, so multi-minute pauses run instantly.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass

import pytest

from agent.turn_connectivity_pause import (
    PauseVerdict,
    enter_connectivity_pause,
    note_transport_failure,
    reset_transport_failures,
)
from agent.turn_retry_state import TurnRetryState


@dataclass
class ProbeOutcome:
    provider_reachable: bool
    network_reachable: bool


class FakeTime:
    """Controllable clock: ``sleep`` advances ``now`` instead of waiting."""

    def __init__(self, now: float = 1000.0):
        self.now = now
        self.slept = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.slept += seconds


class FakeAgent:
    """Minimal turn-loop double: status channels, activity clock, interrupts."""

    def __init__(self):
        self.base_url = "https://api.anthropic.com/v1"
        self.platform = "cli"
        self.log_prefix = "[test] "
        self._interrupt_requested = False
        self.redirect_pending = False
        self.touched = []
        self.statuses = []
        self.waits = []
        self.cleared = []

    def _touch_activity(self, desc, **kwargs):
        self.touched.append(desc)

    def _emit_diagnostic_status(self, message):
        self.statuses.append(message)

    def _emit_diagnostic_wait(self, text):
        self.waits.append(text)

    def clear_interrupt(self, preserve_redirect=False, hard_cancel=False):
        self.cleared.append((preserve_redirect, hard_cancel))
        if preserve_redirect:
            return self.redirect_pending
        self._interrupt_requested = False
        return True


@pytest.fixture
def probe_stub(monkeypatch):
    """Stand-in for the sibling-owned ``agent/connectivity_probe.py``."""
    state = {"kind": "tcp", "outcomes": [], "calls": 0, "hosts_seen": []}

    def classify_transport_failure(exc):
        kind = state["kind"]
        if isinstance(kind, Exception):
            raise kind
        return kind

    def probe_connectivity(provider_host, probe_hosts=None, timeout_s=3.0):
        state["calls"] += 1
        state["hosts_seen"].append(provider_host)
        idx = min(state["calls"] - 1, len(state["outcomes"]) - 1)
        return state["outcomes"][idx]

    mod = types.ModuleType("agent.connectivity_probe")
    mod.ProbeOutcome = ProbeOutcome
    mod.classify_transport_failure = classify_transport_failure
    mod.probe_connectivity = probe_connectivity
    mod._state = state
    monkeypatch.setitem(sys.modules, "agent.connectivity_probe", mod)
    return mod


@pytest.fixture
def fake_time(monkeypatch):
    clock = FakeTime()
    monkeypatch.setattr("agent.turn_connectivity_pause.time", clock)
    return clock


@pytest.fixture
def recovery_stub(monkeypatch):
    """Stand-in for ``agent.turn_recovery`` (unimportable without full deps)."""
    calls = {}

    def abort_turn_on_interrupt(agent, messages, conversation_history, api_call_count,
                                *, abort_message, interrupt_text):
        calls["args"] = (messages, conversation_history, api_call_count)
        return {"final_response": interrupt_text, "messages": messages,
                "api_calls": api_call_count, "completed": False, "interrupted": True}

    mod = types.ModuleType("agent.turn_recovery")
    mod.abort_turn_on_interrupt = abort_turn_on_interrupt
    monkeypatch.setitem(sys.modules, "agent.turn_recovery", mod)
    return calls


def _transported(rs, n=2, kind="tcp"):
    for _ in range(n):
        note_transport_failure(rs, kind)
    return rs


# ---- eligibility -----------------------------------------------------------

def test_disabled_config_returns_continue_without_probing(probe_stub):
    agent = FakeAgent()
    agent._connectivity_pause = {"enabled": False}
    rs = _transported(TurnRetryState())
    probe_stub._state["outcomes"] = [ProbeOutcome(False, False)]
    assert enter_connectivity_pause(agent, ConnectionError("x"), rs).action == "continue"
    assert probe_stub._state["calls"] == 0


def test_non_transport_error_returns_continue(probe_stub):
    agent = FakeAgent()
    rs = _transported(TurnRetryState())
    probe_stub._state["kind"] = "http"
    probe_stub._state["outcomes"] = [ProbeOutcome(False, False)]
    assert enter_connectivity_pause(agent, ValueError("bad"), rs).action == "continue"
    assert probe_stub._state["calls"] == 0


def test_classifier_exception_returns_continue(probe_stub):
    agent = FakeAgent()
    rs = _transported(TurnRetryState())
    probe_stub._state["kind"] = RuntimeError("classifier blew up")
    probe_stub._state["outcomes"] = [ProbeOutcome(False, False)]
    assert enter_connectivity_pause(agent, ConnectionError("x"), rs).action == "continue"
    assert probe_stub._state["calls"] == 0


def test_provider_reachable_returns_continue(probe_stub):
    agent = FakeAgent()
    rs = _transported(TurnRetryState())
    probe_stub._state["outcomes"] = [ProbeOutcome(True, False)]
    assert enter_connectivity_pause(agent, ConnectionError("x"), rs).action == "continue"
    assert probe_stub._state["hosts_seen"] == ["api.anthropic.com"]


def test_provider_outage_returns_continue_for_fallback_ladder(probe_stub):
    agent = FakeAgent()
    rs = _transported(TurnRetryState())
    probe_stub._state["outcomes"] = [ProbeOutcome(False, True)]
    assert enter_connectivity_pause(agent, ConnectionError("x"), rs).action == "continue"


def test_no_provider_host_returns_continue_when_probe_helper_missing(probe_stub):
    # The stub lacks provider_host_for, so the module degrades to a local
    # base_url derivation; with no base_url it cannot confirm local loss.
    agent = FakeAgent()
    agent.base_url = ""
    assert not hasattr(sys.modules["agent.connectivity_probe"], "provider_host_for")
    rs = _transported(TurnRetryState())
    probe_stub._state["outcomes"] = [ProbeOutcome(False, False)]
    assert enter_connectivity_pause(agent, ConnectionError("x"), rs).action == "continue"
    assert probe_stub._state["calls"] == 0


def test_real_probe_module_provider_host_for():
    """Contract with the sibling-owned probe module (no stub): host derivation."""
    import agent.connectivity_probe as cp

    class _WithUrl:
        base_url = "https://api.anthropic.com/v1"

    class _WithoutUrl:
        base_url = ""

    assert cp.provider_host_for(_WithUrl()) == "api.anthropic.com"
    assert cp.provider_host_for(_WithoutUrl()) == "openrouter.ai"


def test_real_probe_module_classify_transport_failure():
    """Contract with the sibling-owned probe module (no stub): classification."""
    import socket

    import agent.connectivity_probe as cp

    assert cp.classify_transport_failure(socket.gaierror("dns fail")) == "dns"
    assert cp.classify_transport_failure(TimeoutError("timed out")) == "timeout"
    assert cp.classify_transport_failure(ConnectionRefusedError("refused")) == "tcp"
    assert cp.classify_transport_failure(RuntimeError("weird")) == "other"


def test_missing_base_url_uses_probe_fallback_host(fake_time, monkeypatch):
    """With the real probe module, a missing base_url falls back to its
    documented last-resort provider host instead of skipping the pause."""
    import agent.connectivity_probe as cp

    agent = FakeAgent()
    agent.base_url = ""
    rs = _transported(TurnRetryState())

    calls = []

    def _flaky_down_then_up(provider_host, probe_hosts=None, timeout_s=3.0):
        calls.append(provider_host)
        return ProbeOutcome(False, False) if len(calls) == 1 else ProbeOutcome(True, True)

    monkeypatch.setattr(cp, "probe_connectivity", _flaky_down_then_up)
    verdict = enter_connectivity_pause(agent, ConnectionRefusedError("refused"), rs)
    assert verdict.action == "continue"
    assert rs.connectivity_pauses == 1
    assert calls == ["openrouter.ai", "openrouter.ai"]


def test_debounce_first_tcp_failure_continues_without_probing(probe_stub):
    agent = FakeAgent()
    rs = _transported(TurnRetryState(), n=1)
    probe_stub._state["outcomes"] = [ProbeOutcome(False, False)]
    assert enter_connectivity_pause(agent, ConnectionError("x"), rs).action == "continue"
    assert probe_stub._state["calls"] == 0


def test_dns_fast_path_pauses_after_one(probe_stub, fake_time):
    agent = FakeAgent()
    rs = _transported(TurnRetryState(), n=1, kind="dns")
    probe_stub._state["kind"] = "dns"
    probe_stub._state["outcomes"] = [ProbeOutcome(False, False), ProbeOutcome(True, True)]
    verdict = enter_connectivity_pause(agent, ConnectionError("dns fail"), rs)
    assert verdict.action == "continue"  # paused, then resumed on short outage
    assert rs.connectivity_pauses == 1
    assert probe_stub._state["calls"] == 2


# ---- pause -> resume --------------------------------------------------------

def test_pause_then_resume_short_outage(probe_stub, fake_time):
    agent = FakeAgent()
    rs = _transported(TurnRetryState())
    probe_stub._state["outcomes"] = [ProbeOutcome(False, False), ProbeOutcome(True, False)]
    verdict = enter_connectivity_pause(agent, ConnectionError("wifi down"), rs)
    assert verdict.action == "continue"
    assert isinstance(verdict, PauseVerdict)
    # keep-guard pins the provider for the resume cycle (/keep contract)
    assert agent._connectivity_resume_keep is True
    assert rs.pause_started_at == pytest.approx(1000.0)
    assert rs.connectivity_pauses == 1
    # visible paused + resumed status, never a message mutation
    assert any("⏸" in s for s in agent.statuses)
    assert any("✅" in s for s in agent.statuses)
    assert any("📶" in w for w in agent.waits)


def test_long_outage_breaks_with_rebuilt_messages(probe_stub, fake_time):
    agent = FakeAgent()
    rs = _transported(TurnRetryState())

    def slow_probe(provider_host, probe_hosts=None, timeout_s=3.0):
        mod_state = sys.modules["agent.connectivity_probe"]._state
        mod_state["calls"] += 1
        if mod_state["calls"] == 2:
            fake_time.now += 2000.0  # the outage outlasts the cache warm window
        return ProbeOutcome(False, False) if mod_state["calls"] == 1 else ProbeOutcome(True, True)

    sys.modules["agent.connectivity_probe"].probe_connectivity = slow_probe
    verdict = enter_connectivity_pause(agent, ConnectionError("wifi down"), rs)
    assert verdict.action == "break"
    assert verdict.restart_with_rebuilt_messages is True
    # mirrors _arm_fallback_restart's arming shape (minus the provider switch)
    assert rs.restart_with_rebuilt_messages is True
    assert rs.primary_recovery_attempted is False
    assert agent._connectivity_resume_keep is True


def test_touch_activity_called_during_pause(probe_stub, fake_time):
    agent = FakeAgent()
    agent._connectivity_pause = {"max_pause_s": 65.0, "probe_interval_s": 10.0}
    rs = _transported(TurnRetryState())
    probe_stub._state["outcomes"] = [ProbeOutcome(False, False)]
    verdict = enter_connectivity_pause(agent, ConnectionError("wifi down"), rs)
    assert verdict.action == "continue"  # cap expired
    assert getattr(agent, "_connectivity_resume_keep", False) is False
    touches = [t for t in agent.touched if "Waiting for network connectivity" in t]
    assert len(touches) == 2  # ~30s and ~60s into the pause


# ---- interrupts ------------------------------------------------------------

def test_stop_interrupt_returns_abort_result(probe_stub, fake_time):
    agent = FakeAgent()
    agent._interrupt_requested = True
    rs = _transported(TurnRetryState())
    probe_stub._state["outcomes"] = [ProbeOutcome(False, False)]
    verdict = enter_connectivity_pause(agent, ConnectionError("wifi down"), rs)
    assert verdict.action == "return"
    assert verdict.result["interrupted"] is True
    assert verdict.result["completed"] is False


def test_steering_redirect_breaks_with_redirect_flag(probe_stub, fake_time):
    agent = FakeAgent()
    agent._interrupt_requested = True
    agent.redirect_pending = True
    rs = _transported(TurnRetryState())
    probe_stub._state["outcomes"] = [ProbeOutcome(False, False)]
    verdict = enter_connectivity_pause(agent, ConnectionError("wifi down"), rs)
    assert verdict.action == "break"
    assert verdict.restart_with_redirected_messages is True
    assert rs.restart_with_redirected_messages is True
    assert verdict.restart_with_rebuilt_messages is False


def test_abort_with_messages_delegates_to_abort_turn_on_interrupt(
    probe_stub, fake_time, recovery_stub
):
    agent = FakeAgent()
    agent._interrupt_requested = True
    rs = _transported(TurnRetryState())
    probe_stub._state["outcomes"] = [ProbeOutcome(False, False)]
    messages = [{"role": "user", "content": "hi"}]
    verdict = enter_connectivity_pause(
        agent, ConnectionError("wifi down"), rs,
        messages=messages, conversation_history=[], api_call_count=3,
    )
    assert verdict.action == "return"
    assert verdict.result["interrupted"] is True
    assert recovery_stub["args"][0] is messages
    assert recovery_stub["args"][2] == 3


# ---- cap -------------------------------------------------------------------

def test_cap_expiry_continues_with_counter_preserved(probe_stub, fake_time):
    agent = FakeAgent()
    agent._connectivity_pause = {"max_pause_s": 1.0, "probe_interval_s": 10.0}
    rs = _transported(TurnRetryState())
    probe_stub._state["outcomes"] = [ProbeOutcome(False, False)]
    verdict = enter_connectivity_pause(agent, ConnectionError("wifi down"), rs)
    assert verdict.action == "continue"
    # counter preserved so the normal recovery ladder sees the real history
    assert rs.consecutive_transport_failures == 2
    assert not any("✅" in s for s in agent.statuses)


def test_cron_effective_cap_is_300s(probe_stub, fake_time):
    agent = FakeAgent()
    agent.platform = "cron"
    agent._connectivity_pause = {"max_pause_s": 1800.0, "probe_interval_s": 10.0}
    rs = _transported(TurnRetryState())
    probe_stub._state["outcomes"] = [ProbeOutcome(False, False)]
    verdict = enter_connectivity_pause(agent, ConnectionError("wifi down"), rs)
    assert verdict.action == "continue"
    assert rs.consecutive_transport_failures == 2
    assert fake_time.slept == pytest.approx(300.0)


def test_non_cron_uses_full_cap(probe_stub, fake_time):
    agent = FakeAgent()
    agent._connectivity_pause = {"max_pause_s": 45.0, "probe_interval_s": 10.0}
    rs = _transported(TurnRetryState())
    probe_stub._state["outcomes"] = [ProbeOutcome(False, False)]
    verdict = enter_connectivity_pause(agent, ConnectionError("wifi down"), rs)
    assert verdict.action == "continue"
    assert fake_time.slept == pytest.approx(50.0)  # 5 probe waits of 10s, then cap


# ---- helpers + state --------------------------------------------------------

def test_note_and_reset_transport_failures():
    rs = TurnRetryState()
    note_transport_failure(rs, "tcp")
    note_transport_failure(rs, "dns")
    assert rs.consecutive_transport_failures == 2
    reset_transport_failures(rs)
    assert rs.consecutive_transport_failures == 0


def test_retry_state_connectivity_field_defaults():
    rs = TurnRetryState()
    assert rs.consecutive_transport_failures == 0
    assert rs.pause_started_at is None
    assert rs.connectivity_pauses == 0
