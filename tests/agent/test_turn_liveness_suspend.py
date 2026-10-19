"""Host suspend must not count as turn inactivity.

Regression: a connectivity pause touches activity every 30s of *awake* time
(time.monotonic, which halts during macOS/Linux suspend) while the liveness
watchdog measured idle with wall-clock time.time(). A lid-close during the
pause (Wi-Fi off) let wall time race ahead ~880s and the watchdog aborted the
paused turn on wake, before the pause loop could resume it.
"""

import threading
import time
from types import SimpleNamespace

from agent.activity_tracking import ActivityTrackingMixin
from agent.turn_liveness import TurnLivenessWatchdog, _idle_seconds


class _Agent(ActivityTrackingMixin):
    session_id = "s"


def _watchdog(agent):
    return TurnLivenessWatchdog(
        agent, session_id="s", timeout_s=600.0, poll_s=15.0,
        stop_event=threading.Event(), activity_lock=agent._liveness_activity_lock(),
        is_turn_active=lambda: True, commit_abort=lambda *_: True, deactivate_turn=lambda: None,
    )


def test_suspend_gap_is_not_idle_time(monkeypatch):
    agent = _Agent()
    agent._touch_activity("📶 Waiting for network connectivity… (1199s elapsed)")
    wall0 = agent._last_activity_ts
    mono0 = agent._last_activity_clock_pair[1]
    # Simulate 900s of sleep: wall clock jumps, monotonic advances only 5s awake.
    monkeypatch.setattr(time, "time", lambda: wall0 + 900.0)
    monkeypatch.setattr(time, "monotonic", lambda: mono0 + 5.0)
    snap = _watchdog(agent)._sample()
    assert 4.9 < snap.idle_seconds < 5.1


def test_awake_stall_still_detected(monkeypatch):
    agent = _Agent()
    agent._touch_activity("tool call")
    wall0 = agent._last_activity_ts
    mono0 = agent._last_activity_clock_pair[1]
    monkeypatch.setattr(time, "time", lambda: wall0 + 900.0)
    monkeypatch.setattr(time, "monotonic", lambda: mono0 + 900.0)
    assert _watchdog(agent)._sample().idle_seconds >= 900.0


def test_direct_ts_write_falls_back_to_wall_clock():
    # gateway/run.py and test doubles write _last_activity_ts directly; the stale
    # pair must be ignored rather than reporting the old monotonic delta.
    agent = SimpleNamespace(_last_activity_clock_pair=(1.0, time.monotonic() - 5000.0))
    agent._last_activity_ts = time.time() - 42.0
    idle = _idle_seconds(agent._last_activity_ts, agent._last_activity_clock_pair)
    assert 41.0 < idle < 44.0
    assert _idle_seconds(None, None) == 0.0
    assert 41.0 < _idle_seconds(time.time() - 42.0, None) < 44.0
