"""Classic-CLI peer-inbox receiver: presence heartbeat + busy→steer / idle→_pending_input."""
import os
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from agent.prompt_builder import PEER_MARKER_PREFIX
from hermes_cli import cli_peer_inbox
from hermes_cli.cli_peer_inbox import CliPeerInboxPoller, peer_messaging_settings, start_cli_peer_inbox_poller


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import peer_inbox
    peer_inbox._presence_cache.clear()
    yield peer_inbox
    peer_inbox._presence_cache.clear()


class _Agent:
    def __init__(self):
        self.calls = []

    def steer(self, text, *, sender=None):
        self.calls.append((text, sender))
        return True


def _cli(**kw):
    base = dict(session_id="cli_rx", _agent_running=False, agent=_Agent(), _pending_input=queue.Queue(),
                _should_exit=False, _pending_title="CLI receiver", _session_db=None, config={})
    return SimpleNamespace(**{**base, **kw})


def _post(inbox, to="cli_rx"):
    res = inbox.post(to, from_session_id="tx_sess", from_title="Sender", from_cwd="/w", body="ping from tx")
    assert res["status"] == "queued", res
    return res["message_id"]


def test_presence_heartbeat_and_session_id_rotation(inbox):
    cli = _cli(_agent_running=True)
    poller = CliPeerInboxPoller(cli)
    poller.tick()
    rec = inbox.read_presence("cli_rx")
    assert rec["surface"] == "cli" and rec["busy"] is True and rec["title"] == "CLI receiver"
    cli.session_id = "cli_rx_new"  # /new or compression
    poller.tick()
    assert inbox.read_presence("cli_rx") is None and inbox.read_presence("cli_rx_new")
    poller.stop()
    assert inbox.read_presence("cli_rx_new") is None


def test_idle_cli_queues_framed_literal_turn(inbox):
    cli = _cli()
    poller = CliPeerInboxPoller(cli)
    poller.tick()
    mid = _post(inbox)
    assert poller.tick() == "delivered_turn"
    item = cli._pending_input.get_nowait()
    text = getattr(item, "text", item)
    assert text.startswith(PEER_MARKER_PREFIX) and "ping from tx" in text
    from cli import _SeededQueryMessage
    assert isinstance(item, _SeededQueryMessage)  # literal: never parsed as /command, ! shell or file drop
    assert inbox.get_receipt("cli_rx", mid)["outcome"] == "delivered_turn"
    assert cli.agent.calls == []


def test_busy_cli_steers_with_sender(inbox):
    cli = _cli(_agent_running=True)
    poller = CliPeerInboxPoller(cli)
    poller.tick()
    mid = _post(inbox)
    assert poller.tick() == "delivered_steer"
    assert cli.agent.calls == [("ping from tx", {"session_id": "tx_sess", "title": "Sender"})]
    assert cli._pending_input.empty()
    assert inbox.get_receipt("cli_rx", mid)["outcome"] == "delivered_steer"


def test_busy_cli_without_agent_leaves_message_queued(inbox):
    cli = _cli(_agent_running=True)
    cli.agent = None
    poller = CliPeerInboxPoller(cli)
    poller.tick()
    mid = _post(inbox)
    assert poller.tick() is None
    assert inbox.has_pending("cli_rx") and inbox.get_receipt("cli_rx", mid) is None


def test_settings_and_refuse(inbox):
    assert peer_messaging_settings({}) == (True, "accept")
    assert peer_messaging_settings({"peer_messaging": {"enabled": "off", "inbound": "REFUSE"}}) == (False, "refuse")
    cli = _cli()
    poller = CliPeerInboxPoller(cli, inbound="refuse")
    poller.tick()
    res = inbox.post("cli_rx", from_session_id="tx", from_title="t", from_cwd="/w", body="hi")
    assert res["status"] == "refused"
    cli.config = {"peer_messaging": {"enabled": False}}
    assert start_cli_peer_inbox_poller(cli) is None


def test_thread_delivers_then_clears_presence_on_exit(inbox, monkeypatch):
    monkeypatch.setattr(cli_peer_inbox.atexit, "register", lambda fn: None)
    cli = _cli()
    poller = CliPeerInboxPoller(cli, interval=0.02).start()
    deadline = time.time() + 3
    while inbox.read_presence("cli_rx") is None and time.time() < deadline:
        time.sleep(0.02)
    _post(inbox)
    item = cli._pending_input.get(timeout=3)
    assert getattr(item, "text", item).startswith(PEER_MARKER_PREFIX)
    cli._should_exit = True
    poller._thread.join(timeout=3)
    assert not poller._thread.is_alive() and inbox.read_presence("cli_rx") is None
