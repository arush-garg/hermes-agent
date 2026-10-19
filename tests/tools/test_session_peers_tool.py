"""sessions_peers tool: list joins registry + state.db + presence; send resolves targets and waits briefly."""
import json
import os
import threading
import time

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from tools import peer_inbox, session_peers_tool as tool
    peer_inbox._presence_cache.clear()
    monkeypatch.setattr(tool, "SEND_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(tool, "SEND_POLL_INTERVAL", 0.02)

    from hermes_state import SessionDB
    db = SessionDB(db_path=tmp_path / "state.db")
    sessions = {
        "20261019_me_aaaaaa": ("Me", "/me"),
        "20261019_p1_bbbbbb": ("Refactor auth", "/repo/a"),
        "20261019_p2_cccccc": ("Docs", "/repo/b"),
        "20261019_p3_xxcccc": (None, "/repo/c"),  # untitled → falls back to presence title
    }
    for sid, (title, cwd) in sessions.items():
        db.create_session(sid, "cli", cwd=cwd)
        if title:
            db.set_session_title(sid, title)
    db.close()
    entries = [{"lease_id": f"lease-{i}", "session_id": sid, "pid": os.getpid(), "surface": "tui",
                "metadata": {"live_session_id": sid}, "started_at": time.time(), "updated_at": time.time()}
               for i, sid in enumerate(list(sessions) + ["20261019_p1_bbbbbb"])]  # duplicate lease → deduped
    reg = tmp_path / "runtime" / "active_sessions.json"
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(json.dumps({"entries": entries}))
    for sid in sessions:
        peer_inbox.write_presence(sid, pid=os.getpid(), surface="tui", busy=sid.endswith("bbbbbb"),
                                  title="presence-title")
    yield tool, peer_inbox
    peer_inbox._presence_cache.clear()


ME = "20261019_me_aaaaaa"


def _call(tool, **args):
    return json.loads(tool.handle_sessions_peers(args, session_id=ME))


def test_list_joins_and_excludes_self(env):
    tool, _ = env
    out = _call(tool, action="list")
    assert out["self"] == ME
    by_id = {s["id"]: s for s in out["sessions"]}
    assert set(by_id) == {"20261019_p1_bbbbbb", "20261019_p2_cccccc", "20261019_p3_xxcccc"}
    p1 = by_id["20261019_p1_bbbbbb"]
    assert p1 == {"id": "20261019_p1_bbbbbb", "short": "bbbbbb", "surface": "tui", "title": "Refactor auth",
                  "cwd": "/repo/a", "busy": True, "can_receive": True, "inbound": "accept",
                  "last_activity": p1["last_activity"]}


def test_list_marks_peers_without_presence(env):
    tool, inbox = env
    inbox.clear_presence("20261019_p1_bbbbbb")
    p1 = {s["id"]: s for s in _call(tool, action="list")["sessions"]}["20261019_p1_bbbbbb"]
    assert p1["can_receive"] is False and p1["busy"] is None


@pytest.mark.parametrize("to", ["20261019_p1_bbbbbb", "bbbbbb", "Refactor auth"])
def test_send_resolves_and_queues(env, to):
    tool, inbox = env
    out = _call(tool, action="send", to=to, message="found the bug in auth.py:42")
    assert out["status"] == "queued" and out["to"] == "20261019_p1_bbbbbb"
    env_ = inbox.claim_next("20261019_p1_bbbbbb")
    assert env_["id"] == out["message_id"] and env_["from_session_id"] == ME and env_["from_title"] == "Me"


def test_send_ambiguous_refused_with_candidates(env):
    tool, inbox = env
    out = _call(tool, action="send", to="cccc", message="hi")
    assert out["status"] == "refused" and "ambiguous" in out["detail"]
    assert {c["id"] for c in out["candidates"]} == {"20261019_p2_cccccc", "20261019_p3_xxcccc"}
    assert not inbox.has_pending("20261019_p2_cccccc")


def test_send_unknown_and_self_refused(env):
    tool, _ = env
    assert _call(tool, action="send", to="nope", message="hi")["status"] == "refused"
    assert _call(tool, action="send", to=ME, message="hi")["status"] == "refused"
    assert _call(tool, action="send", to="bbbbbb", message="  ")["status"] == "refused"


def test_send_delivered_when_receiver_completes(env):
    tool, inbox = env
    target = "20261019_p2_cccccc"

    def receiver():
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            msg = inbox.claim_next(target)
            if msg:
                inbox.complete(target, msg["id"], "delivered_steer")
                return
            time.sleep(0.01)

    t = threading.Thread(target=receiver)
    t.start()
    out = _call(tool, action="send", to=target, message="ping")
    t.join()
    assert out["status"] == "delivered" and out["message_id"].startswith("pm_")


def test_send_failed_receipt_reports_refused(env):
    tool, inbox = env
    target = "20261019_p2_cccccc"

    def receiver():
        for _ in range(500):
            msg = inbox.claim_next(target)
            if msg:
                inbox.complete(target, msg["id"], "failed", "agent gone")
                return
            time.sleep(0.01)

    t = threading.Thread(target=receiver)
    t.start()
    out = _call(tool, action="send", to=target, message="ping")
    t.join()
    assert out["status"] == "refused" and "agent gone" in out["detail"]


def test_bad_action(env):
    tool, _ = env
    assert "error" in _call(tool, action="nope")


def test_registered_and_in_default_cli_but_blocked_for_children():
    import tools.session_peers_tool  # noqa: F401  (registers)
    from tools.registry import registry
    from toolsets import resolve_toolset, _HERMES_CORE_TOOLS
    from tools.delegate_tool_toolsets import DELEGATE_BLOCKED_TOOLS, _strip_blocked_tools

    assert registry.get_toolset_for_tool("sessions_peers") == "sessions_peers"
    assert "sessions_peers" in _HERMES_CORE_TOOLS
    assert "sessions_peers" in resolve_toolset("hermes-cli")
    assert "sessions_peers" in DELEGATE_BLOCKED_TOOLS
    assert "sessions_peers" not in _strip_blocked_tools(["sessions_peers", "web"])


def test_presence_only_session_is_listed_and_sendable(env):
    """A TUI session publishes presence before it takes a registry lease (lease is lazy on first prompt)."""
    tool, peer_inbox = env
    lazy = "20261019_tui_dddddd"
    peer_inbox.write_presence(lazy, pid=os.getpid(), surface="tui", busy=False, title="fresh tui")
    out = _call(tool, action="list")
    row = {s["id"]: s for s in out["sessions"]}[lazy]
    assert row["can_receive"] is True and row["title"] == "fresh tui" and row["surface"] == "tui"
    sent = _call(tool, action="send", to="dddddd", message="hi")
    assert sent["status"] == "queued" and sent["to"] == lazy
