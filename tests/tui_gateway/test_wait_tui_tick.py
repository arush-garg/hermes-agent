"""/wait firing from the TUI/Desktop session-owner process: the slash worker only stores the wait."""

from __future__ import annotations

import importlib
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def server(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import goals

    goals._DB_CACHE.clear()
    with patch.dict("sys.modules", {"hermes_cli.env_loader": MagicMock(), "hermes_cli.banner": MagicMock()}):
        mod = importlib.import_module("tui_gateway.server")
        yield mod
        mod._sessions.clear()
    goals._DB_CACHE.clear()


@pytest.fixture()
def session(server):
    sid, key = "sid-wait-test", "tui-wait-session-1"
    s = {"session_key": key, "history": [], "history_lock": threading.Lock(), "history_version": 0,
         "running": False, "attached_images": [], "cols": 120, "agent": MagicMock()}
    server._sessions[sid] = s
    return sid, key, s


@pytest.mark.parametrize("started", [True, False])
def test_due_wait_is_submitted_verbatim_or_stays_due_when_refused(server, session, started):
    from hermes_cli.waits import WaitManager, load_waits

    sid, key, s = session
    WaitManager(key).add("continue working on this", 60, now=time.time() - 120)
    WaitManager(key).add("later", 3600)

    def submit(rid, sid_, session_, text, **kw):
        if not started:
            with s["history_lock"]:
                s["running"] = False  # _admit_prompt_turn refusal releases itself and returns False
        return started

    submit = MagicMock(side_effect=submit)
    with patch.object(server, "_run_prompt_submit", submit), patch.object(server, "_emit"):
        server._maybe_fire_tui_wait_tick(sid, s)
    assert submit.call_args.args[3] == "continue working on this"
    assert s["running"] is started
    remaining = sorted(w.prompt for w in load_waits(key).waits)
    assert remaining == (["later"] if started else ["continue working on this", "later"])
