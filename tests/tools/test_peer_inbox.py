"""peer_inbox transport invariants on real disk (isolated HERMES_HOME per test)."""
import os
import stat
import subprocess
import sys
import time

import pytest


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import peer_inbox
    peer_inbox._presence_cache.clear()
    yield peer_inbox
    peer_inbox._presence_cache.clear()


def _live(inbox, sid="rx", **kw):
    inbox.write_presence(sid, pid=os.getpid(), surface="cli", busy=False, **kw)


def _post(inbox, to="rx", frm="tx", body="hello"):
    return inbox.post(to, from_session_id=frm, from_title="Sender", from_cwd="/w", body=body)


def test_post_claim_fifo_complete_receipt(inbox, tmp_path):
    _live(inbox)
    ids = [_post(inbox, body=f"m{i}")["message_id"] for i in range(3)]
    assert all(i.startswith("pm_") for i in ids)
    assert inbox.has_pending("rx")
    claimed = [inbox.claim_next("rx") for _ in range(3)]
    assert [c["id"] for c in claimed] == ids
    assert [c["body"] for c in claimed] == ["m0", "m1", "m2"]
    assert set(claimed[0]) == {"id", "from_session_id", "from_title", "from_cwd", "to_session_id", "body",
                               "created_at"}
    assert claimed[0]["from_session_id"] == "tx" and claimed[0]["to_session_id"] == "rx"
    assert inbox.claim_next("rx") is None and not inbox.has_pending("rx")
    assert inbox.get_receipt("rx", ids[0]) is None
    inbox.complete("rx", ids[0], "delivered_steer")
    inbox.complete("rx", ids[0], "failed", "late duplicate")  # first outcome wins
    receipt = inbox.get_receipt("rx", ids[0])
    assert receipt["outcome"] == "delivered_steer"
    box = tmp_path / "runtime" / "peer_inbox" / "rx"
    assert not (box / "claimed" / f"{ids[0]}.json").exists()
    assert (box / "claimed" / f"{ids[1]}.json").exists()
    with pytest.raises(ValueError):
        inbox.complete("rx", ids[1], "bogus")


def test_claim_is_at_most_once_across_processes(inbox, tmp_path):
    _live(inbox)
    for i in range(6):
        _post(inbox, body=f"m{i}")
    script = ("import json; from tools.peer_inbox import claim_next\n"
              "out=[]\nwhile True:\n e=claim_next('rx')\n if e is None: break\n out.append(e['id'])\n"
              "print(json.dumps(out))")
    env = dict(os.environ, HERMES_HOME=str(tmp_path))
    procs = [subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, env=env, cwd=os.getcwd()) for _ in range(3)]
    import json
    seen = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        assert p.returncode == 0, err
        seen += json.loads(out.strip().splitlines()[-1])
    assert len(seen) == 6 and len(set(seen)) == 6


def test_refusals(inbox):
    assert _post(inbox)["reason"] == inbox.NO_PRESENCE_REASON  # no presence
    _live(inbox)
    assert _post(inbox, to="rx", frm="rx")["status"] == "refused"  # self-send
    assert _post(inbox, body="   ")["status"] == "refused"
    assert _post(inbox, to="../x")["status"] == "refused"
    inbox._presence_cache.clear()
    _live(inbox, inbound="refuse")
    r = _post(inbox)
    assert r["status"] == "refused" and "not accepting" in r["reason"]


def test_rate_limit_per_pair(inbox):
    _live(inbox)
    for _ in range(inbox.RATE_LIMIT_COUNT):
        assert _post(inbox)["status"] == "queued"
    limited = _post(inbox)
    assert limited["status"] == "refused" and "rate limit" in limited["reason"]
    assert _post(inbox, frm="other")["status"] == "queued"  # other pair unaffected


def test_stale_or_dead_presence_is_not_live(inbox, tmp_path, monkeypatch):
    _live(inbox)
    assert inbox.read_presence("rx")["pid"] == os.getpid()
    real = time.time()
    monkeypatch.setattr(inbox.time, "time", lambda: real + inbox.PRESENCE_TTL_SECONDS + 5)
    assert inbox.read_presence("rx") is None
    monkeypatch.undo()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    inbox.write_presence("dead", pid=dead.pid, surface="tui", busy=True)
    assert inbox.read_presence("dead") is None
    assert _post(inbox, to="dead")["reason"] == inbox.NO_PRESENCE_REASON
    inbox.clear_presence("rx")
    assert inbox.read_presence("rx") is None


def test_presence_write_is_throttled(inbox, tmp_path):
    _live(inbox)
    path = tmp_path / "runtime" / "peer_inbox" / "rx" / "presence.json"
    first = path.read_text()
    _live(inbox)
    assert path.read_text() == first  # only time would change → skipped
    inbox.write_presence("rx", pid=os.getpid(), surface="cli", busy=True)
    assert '"busy": true' in path.read_text()


def test_permissions(inbox, tmp_path):
    _live(inbox)
    mid = _post(inbox)["message_id"]
    inbox.claim_next("rx")
    inbox.complete("rx", mid, "delivered_turn")
    root = tmp_path / "runtime" / "peer_inbox"
    for path in [root, *root.rglob("*")]:
        mode = stat.S_IMODE(path.stat().st_mode)
        if path.is_dir():
            assert mode == 0o700, path
        else:
            assert mode == 0o600, path


def test_gc_removes_old_records_and_empty_dead_boxes(inbox, tmp_path):
    _live(inbox)
    mid = _post(inbox)["message_id"]
    inbox.claim_next("rx")
    inbox.complete("rx", mid, "delivered_turn")
    assert inbox.gc(max_age_seconds=3600) == 0
    assert inbox.gc(max_age_seconds=-1) >= 1
    assert inbox.get_receipt("rx", mid) is None
    assert (tmp_path / "runtime" / "peer_inbox" / "rx").is_dir()  # live presence keeps the box
    inbox.clear_presence("rx")
    inbox.gc(max_age_seconds=-1)
    assert not (tmp_path / "runtime" / "peer_inbox" / "rx").exists()
