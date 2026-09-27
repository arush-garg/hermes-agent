"""/wait: one-shot deferred user messages (hermes_cli/waits.py) and their CLI driver."""

import queue
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli import goals, heartbeat
from hermes_cli.cli_loops_mixin import CLILoopsMixin
from hermes_cli.heartbeat import MIN_INTERVAL_SECONDS, parse_duration, parse_interval
from hermes_cli.waits import WaitManager, load_waits, migrate_waits_to_session, run_wait_command


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()


@pytest.mark.parametrize("text", ["10m", "2h", "2 hours", "90 minutes", "1.5d", "600s", "30s", "banana", "m10", ""])
def test_heartbeat_interval_is_the_wait_duration_grammar_plus_its_floor(text):
    """One grammar: every duration /wait accepts is the heartbeat interval with ``every`` optional, and only the
    heartbeat's re-entry floor tells them apart."""
    seconds = parse_duration(text)
    expected = None if seconds is None else (-1 if seconds < MIN_INTERVAL_SECONDS else seconds)
    assert parse_interval(text) == expected
    assert parse_interval(f"every {text}") == expected


def test_wait_sends_its_message_verbatim_exactly_once_after_the_delay(hermes_home):
    """Set in one process (a slash worker), claimed in another (the session owner): the store is the contract."""
    now = time.time()
    reply, armed = run_wait_command(WaitManager("s1"), "2h continue working on this")
    assert armed and "continue working on this" in reply
    owner = WaitManager("s1")
    assert owner.due_prompt(now + 7200 - 5) is None
    assert owner.due_prompt(now + 7200 + 1) == "continue working on this"
    assert owner.due_prompt(now + 7200 + 2) is None
    assert not WaitManager("s1").is_active()


def test_refund_keeps_an_unstarted_wait_due_but_never_resurrects_a_cancelled_one(hermes_home):
    mgr = WaitManager("s2")
    mgr.add("ping me", 60, now=time.time() - 120)
    assert mgr.due_prompt() == "ping me"
    assert mgr.abandon_fire() is True  # turn never started: the wait stays due
    assert WaitManager("s2").due_prompt() == "ping me"

    mgr.add("second", 60, now=time.time() - 120)
    assert mgr.due_prompt() == "second"
    run_wait_command(WaitManager("s2"), "cancel all")  # lands between the claim and the refund
    assert mgr.abandon_fire() is False
    third = mgr.add("third", 60, now=time.time() - 120)
    assert mgr.due_prompt() == "third"
    run_wait_command(WaitManager("s2"), f"cancel {third.id}")
    assert mgr.abandon_fire() is False
    assert not load_waits("s2").waits


@pytest.mark.parametrize("args", ["continue working", "2h", "0s go", "2h /compress", "cancel x"])
def test_malformed_wait_stores_nothing(hermes_home, args):
    reply, armed = run_wait_command(WaitManager("s3"), args)
    assert not armed and reply
    assert not load_waits("s3").waits


def test_pending_waits_follow_a_compression_rotation(hermes_home):
    WaitManager("parent").add("resume the refactor", 3600)
    assert migrate_waits_to_session("parent", "child")
    assert not WaitManager("parent").is_active()
    assert [w.prompt for w in WaitManager("child").pending] == ["resume the refactor"]


class _Cli(CLILoopsMixin):
    def __init__(self, session_id):
        self.session_id = session_id
        self._pending_input = queue.Queue()
        self._agent_running = False
        self._should_exit = False

    def _get_heartbeat_manager(self):
        return None


def test_cli_watchdog_queues_a_due_wait_as_the_next_user_input(hermes_home, monkeypatch):
    monkeypatch.setattr(heartbeat, "POLL_SECONDS", 0.05)
    cli = _Cli("cli-wait")
    try:
        with patch("cli._cprint"):
            cli._handle_wait_command("/wait 1s continue working on this")
        assert cli._pending_input.get(timeout=5) == "continue working on this"
        time.sleep(0.3)
        assert cli._pending_input.empty()
    finally:
        cli._should_exit = True


def test_slash_worker_never_claims_waits_into_its_undrained_queue(hermes_home, monkeypatch):
    """The TUI slash worker parses /wait but no turn loop drains its queue: the session owner must get the wait."""
    monkeypatch.setattr(heartbeat, "POLL_SECONDS", 0.05)
    cli = _Cli("worker-wait")
    cli._headless_slash_worker = True
    try:
        with patch("cli._cprint"):
            cli._handle_wait_command("/wait 1s continue working on this")
        time.sleep(1.5)
        assert cli._pending_input.empty()
        assert WaitManager("worker-wait").is_due()
    finally:
        cli._should_exit = True
