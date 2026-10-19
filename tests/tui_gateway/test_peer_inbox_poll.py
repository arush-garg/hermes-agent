"""TUI/desktop peer-inbox receiver: presence heartbeat + busy→steer / idle→next-turn delivery."""
import os
import threading
import time
import uuid
from types import SimpleNamespace

import pytest

from agent.prompt_builder import PEER_MARKER_PREFIX
from tui_gateway import session_notifications
from tui_gateway.method_ctx import rebind
from tui_gateway.session_lifecycle import _session_turn_admission


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import peer_inbox
    peer_inbox._presence_cache.clear()
    yield peer_inbox
    peer_inbox._presence_cache.clear()


class _Steerable:
    def __init__(self, accept=True):
        self.calls, self.accept = [], accept

    def steer(self, text, *, sender=None):
        self.calls.append((text, sender))
        return self.accept


def _bound_globals(overrides: dict) -> dict:
    """Module namespace with every peer helper rebound onto it (as server.py's bind_module does)."""
    g = {**vars(session_notifications), **overrides}
    for name in ("_peer_messaging_settings", "_peer_presence_title", "_peer_inbox_release",
                 "_peer_agent_accepts_sender", "_poll_peer_inbox_once", "_poll_peer_inbox_guarded"):
        if name not in overrides:
            g[name] = rebind(getattr(session_notifications, name), g)
    return g


def _poll(cfg=None, **overrides):
    enqueued, drained, builds = [], [], []

    def enqueue(session, text, transport, **_):
        enqueued.append(text)
        session["queued_prompt"] = {"text": text, "transport": transport}

    g = {
        "os": os, "time": time, "threading": threading, "uuid": uuid, "contextlib": __import__("contextlib"),
        "logger": session_notifications.__dict__.get("logger") or __import__("logging").getLogger("t"),
        "_load_cfg": lambda: cfg or {}, "_session_live_title": lambda session, key: "Receiver title",
        "_session_uses_compute_host": lambda session: False,
        "_session_compression_in_flight": lambda session: False,
        "_session_turn_admission": _session_turn_admission,
        "_start_agent_build": lambda sid, session: builds.append(sid),
        "_enqueue_prompt": enqueue,
        "_drain_queued_prompt": lambda rid, sid, session: drained.append((rid, sid)),
        **overrides,
    }
    g = _bound_globals(g)
    poll, release = g["_poll_peer_inbox_once"], g["_peer_inbox_release"]
    return poll, release, enqueued, drained, builds


def _session(**kw):
    return {"history_lock": threading.RLock(), "session_key": "rx_durable", "agent": _Steerable(),
            "cwd": "/work/rx", **kw}


def _post(inbox, body="please look at auth.py"):
    res = inbox.post("rx_durable", from_session_id="tx_sess", from_title="Sender tab", from_cwd="/w", body=body)
    assert res["status"] == "queued", res
    return res["message_id"]


def test_presence_keyed_on_session_key_with_busy_flag(inbox):
    poll, release, *_ = _poll()
    session = _session(running=True)
    assert poll("ui-ephemeral", session) is None
    rec = inbox.read_presence("rx_durable")
    assert rec and rec["busy"] is True and rec["surface"] == "tui" and rec["pid"] == os.getpid()
    assert rec["title"] == "Receiver title" and rec["cwd"] == "/work/rx" and rec["inbound"] == "accept"
    assert inbox.read_presence("ui-ephemeral") is None
    session["running"] = False
    poll("ui-ephemeral", session)
    assert inbox.read_presence("rx_durable")["busy"] is False
    # compression rotates the durable key: old presence cleared, new one written
    session["session_key"] = "rx_rotated"
    poll("ui-ephemeral", session)
    assert inbox.read_presence("rx_durable") is None and inbox.read_presence("rx_rotated")
    release(session)
    assert inbox.read_presence("rx_rotated") is None


def test_idle_session_gets_framed_next_turn_and_delivered_turn_receipt(inbox):
    poll, _, enqueued, drained, _ = _poll()
    session = _session()
    poll("ui", session)  # presence first so post() admits
    mid = _post(inbox)
    assert poll("ui", session) == "delivered_turn"
    assert len(enqueued) == 1 and enqueued[0].startswith(PEER_MARKER_PREFIX)
    assert "please look at auth.py" in enqueued[0] and "tx_sess" in enqueued[0]
    for _ in range(50):
        if drained:
            break
        time.sleep(0.01)
    assert drained and drained[0][1] == "ui"
    assert inbox.get_receipt("rx_durable", mid)["outcome"] == "delivered_turn"
    assert session["agent"].calls == []


def test_running_session_steers_raw_body_with_sender(inbox):
    poll, _, enqueued, _, _ = _poll()
    session = _session(running=True)
    poll("ui", session)
    mid = _post(inbox)
    assert poll("ui", session) == "delivered_steer"
    assert session["agent"].calls == [("please look at auth.py", {"session_id": "tx_sess", "title": "Sender tab"})]
    assert not enqueued
    assert inbox.get_receipt("rx_durable", mid)["outcome"] == "delivered_steer"


def test_rejected_steer_records_failed_receipt(inbox):
    poll, *_ = _poll()
    session = _session(running=True, agent=_Steerable(accept=False))
    poll("ui", session)
    mid = _post(inbox)
    assert poll("ui", session) == "failed"
    assert inbox.get_receipt("rx_durable", mid)["outcome"] == "failed"


@pytest.mark.parametrize("state", [
    {"running": True, "agent": None},                 # turn-build window
    {"running": True, "_compute_host": True},          # isolated compute-host turn: steer not proxied
    {"queued_prompt": {"text": "user follow-up"}},     # a local follow-up owns the idle boundary
])
def test_message_stays_queued_when_delivery_must_wait(inbox, state):
    compute = state.pop("_compute_host", False)
    poll, _, enqueued, _, _ = _poll(_session_uses_compute_host=lambda session: compute)
    session = _session(**state)
    poll("ui", session)
    mid = _post(inbox)
    assert poll("ui", session) is None
    assert inbox.has_pending("rx_durable") and inbox.get_receipt("rx_durable", mid) is None
    assert not enqueued


def test_idle_without_agent_kicks_build_and_waits(inbox):
    poll, _, enqueued, _, builds = _poll()
    session = _session(agent=None, agent_ready=threading.Event())
    poll("ui", session)
    _post(inbox)
    assert poll("ui", session) is None and builds == ["ui"] and inbox.has_pending("rx_durable")
    session["agent"] = _Steerable()
    assert poll("ui", session) == "delivered_turn" and len(enqueued) == 1


def test_closing_session_clears_presence_and_does_not_claim(inbox):
    poll, *_ = _poll()
    session = _session()
    poll("ui", session)
    _post(inbox)
    session["_closing"] = True
    assert poll("ui", session) is None
    assert inbox.read_presence("rx_durable") is None and inbox.has_pending("rx_durable")


def test_inbound_refuse_makes_post_refuse(inbox):
    poll, *_ = _poll(cfg={"peer_messaging": {"inbound": "refuse"}})
    session = _session()
    poll("ui", session)
    assert inbox.read_presence("rx_durable")["inbound"] == "refuse"
    res = inbox.post("rx_durable", from_session_id="tx_sess", from_title="t", from_cwd="/w", body="hi")
    assert res["status"] == "refused"


def test_disabled_writes_no_presence(inbox):
    poll, *_ = _poll(cfg={"peer_messaging": {"enabled": False}})
    session = _session()
    assert poll("ui", session) is None
    assert inbox.read_presence("rx_durable") is None


def test_guarded_poll_throttles_warnings(caplog):
    calls = []

    def boom(sid, session):
        calls.append(sid)
        raise RuntimeError("disk gone")
    log = SimpleNamespace(warnings=[], warning=lambda *a, **k: log.warnings.append(a), debug=lambda *a, **k: None)
    guarded = _bound_globals({"_poll_peer_inbox_once": boom, "logger": log})["_poll_peer_inbox_guarded"]
    session = {}
    for t in (100.0, 101.0, 102.0):
        guarded("ui", session, t)
    assert len(calls) == 3 and len(log.warnings) == 1 and session["_peer_poll_warn_suppressed"] == 2
    guarded("ui", session, 200.0)
    assert len(log.warnings) == 2


def test_server_binds_peer_poller():
    from tui_gateway import server
    assert server._PEER_INBOX_POLL_SECONDS == 1.0
    assert callable(server._poll_peer_inbox_once) and callable(server._poll_peer_inbox_guarded)
