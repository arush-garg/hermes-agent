"""Cross-session peer inbox: a durable, at-most-once mailbox between live Hermes sessions.

Layout (dirs 0700, files 0600)::

    <HERMES_HOME>/runtime/peer_inbox/<session_id>/
        presence.json       receiver heartbeat (pid, surface, busy, inbound policy, title, cwd)
        rate.json           per-sender send timestamps (rate limit, persisted under the lock)
        queue/<seq>-<id>.json   admitted, not yet claimed (FIFO by name)
        claimed/<id>.json       claimed by the receiver; never re-queued (at-most-once)
        done/<id>.json          terminal receipt (delivered_steer | delivered_turn | failed)

A message advances queue -> claimed -> done under the per-session file lock, mirroring
tools/bot_live_delivery.py. A crashed receiver leaves an inspectable claimed record, never a
second delivery.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from hermes_cli.active_sessions import _FileLock
from hermes_constants import get_hermes_home
from utils import atomic_json_write, fsync_directory

log = logging.getLogger(__name__)

INBOX_DIR_NAME = "peer_inbox"
PRESENCE_TTL_SECONDS = 120.0
PRESENCE_REWRITE_SECONDS = 30.0
RATE_LIMIT_COUNT = 10
RATE_LIMIT_WINDOW_SECONDS = 60.0
MAX_BODY_CHARS = 32_000  # hard admission cap; the receiver's sanitizer trims further for display
OUTCOMES = frozenset({"delivered_steer", "delivered_turn", "failed"})
INBOUND_POLICIES = frozenset({"accept", "refuse"})

_SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,159}")
_MESSAGE_ID_RE = re.compile(r"pm_[0-9a-f]{32}")
_SUBDIRS = ("queue", "claimed", "done")

# Process-local presence cache: session_id -> (fields-without-time, monotonic time of last write).
_presence_cache: dict[str, tuple[tuple, float]] = {}

NO_PRESENCE_REASON = "target is not running a peer-messaging-capable Hermes build"


def _root() -> Path:
    return Path(get_hermes_home()) / "runtime" / INBOX_DIR_NAME


def _sid(session_id: Any) -> str:
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id) or ".." in session_id:
        raise ValueError(f"invalid session id: {session_id!r}")
    return session_id


def _mid(message_id: Any) -> str:
    if not isinstance(message_id, str) or not _MESSAGE_ID_RE.fullmatch(message_id):
        raise ValueError(f"invalid message id: {message_id!r}")
    return message_id


def _private_dir(path: Path) -> None:
    created = not path.is_dir()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    if created:
        fsync_directory(path.parent)


def _session_dir(session_id: str, *, create: bool) -> Path:
    root = _root()
    path = root / _sid(session_id)
    if create:
        root.parent.mkdir(parents=True, exist_ok=True)
        _private_dir(root)
        _private_dir(path)
        for sub in _SUBDIRS:
            _private_dir(path / sub)
    return path


@contextmanager
def _locked(session_id: str):
    path = _session_dir(session_id, create=True)
    lock = path / ".lock"
    os.close(os.open(lock, os.O_CREAT | os.O_WRONLY, 0o600))
    with _FileLock(lock):
        yield path


def _write(path: Path, data: dict[str, Any]) -> None:
    atomic_json_write(path, data, mode=0o600, fsync_dir=True)


def _read(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("peer inbox: unreadable record %s: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------

def _start_time_for(pid: int) -> float | None:
    from hermes_cli import active_sessions as _as
    return _as._own_start_time() if pid == os.getpid() else _as._process_start_time(pid)


def write_presence(session_id: str, *, pid: int, surface: str, busy: bool, inbound: str = "accept",
                   title: str = "", cwd: str = "") -> None:
    """Heartbeat the receiver. Skips the disk write when only the timestamp would change and
    the last write is younger than PRESENCE_REWRITE_SECONDS (pollers call this every ~1s)."""
    sid = _sid(session_id)
    if inbound not in INBOUND_POLICIES:
        raise ValueError(f"inbound must be one of {sorted(INBOUND_POLICIES)}")
    fields = (int(pid), str(surface or ""), bool(busy), inbound, str(title or "")[:200], str(cwd or ""))
    now_mono = time.monotonic()
    cached = _presence_cache.get(sid)
    path = _session_dir(sid, create=False) / "presence.json"
    if cached and cached[0] == fields and now_mono - cached[1] < PRESENCE_REWRITE_SECONDS and path.is_file():
        return
    path = _session_dir(sid, create=True) / "presence.json"
    _write(path, {
        "session_id": sid, "pid": fields[0], "process_start_time": _start_time_for(fields[0]),
        "surface": fields[1], "busy": fields[2], "inbound": fields[3], "title": fields[4],
        "cwd": fields[5], "updated_at": time.time(),
    })
    _presence_cache[sid] = (fields, now_mono)


def _presence_is_live(record: dict[str, Any] | None, *, now: float | None = None) -> bool:
    if not record:
        return False
    from hermes_cli.active_sessions import _pid_liveness
    try:
        updated = float(record.get("updated_at") or 0)
    except (TypeError, ValueError):
        return False
    if (now if now is not None else time.time()) - updated > PRESENCE_TTL_SECONDS:
        return False
    return bool(_pid_liveness(record.get("pid"), record.get("process_start_time"), lenient=True))


def read_presence(session_id: str) -> dict | None:
    """Live presence record, or None when missing, stale (>120s), or its pid is dead/recycled."""
    try:
        path = _session_dir(session_id, create=False) / "presence.json"
    except ValueError:
        return None
    record = _read(path)
    return record if _presence_is_live(record) else None


def live_presence_ids() -> list[str]:
    """Session ids with a live presence record (pid alive, heartbeat fresh)."""
    root = _root()
    if not root.is_dir():
        return []
    out = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and _presence_is_live(_read(child / "presence.json")):
            out.append(child.name)
    return out


def clear_presence(session_id: str) -> None:
    sid = _sid(session_id)
    _presence_cache.pop(sid, None)
    try:
        (_session_dir(sid, create=False) / "presence.json").unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Mailbox
# ---------------------------------------------------------------------------

def _refused(reason: str) -> dict[str, str]:
    return {"status": "refused", "reason": reason}


def post(to_session_id: str, *, from_session_id: str, from_title: str, from_cwd: str, body: str) -> dict:
    """Admit one message for ``to_session_id``: ``{"status":"queued","message_id"}`` or
    ``{"status":"refused","reason"}``."""
    try:
        to_sid, from_sid = _sid(to_session_id), _sid(from_session_id)
    except ValueError as exc:
        return _refused(str(exc))
    if to_sid == from_sid:
        return _refused("cannot send a peer message to this same session")
    body = body if isinstance(body, str) else ""
    if not body.strip():
        return _refused("message body is empty")
    if len(body) > MAX_BODY_CHARS:
        return _refused(f"message too long ({len(body)} chars; max {MAX_BODY_CHARS}) — pass file paths instead")
    presence = read_presence(to_sid)
    if presence is None:
        return _refused(NO_PRESENCE_REASON)
    if presence.get("inbound") == "refuse":
        return _refused("target session is not accepting peer messages")
    with _locked(to_sid) as box:
        now = time.time()
        rate_path = box / "rate.json"
        rate = _read(rate_path) or {}
        recent = [t for t in rate.get(from_sid, []) if isinstance(t, (int, float)) and now - t < RATE_LIMIT_WINDOW_SECONDS]
        if len(recent) >= RATE_LIMIT_COUNT:
            return _refused(
                f"rate limit: more than {RATE_LIMIT_COUNT} messages to this session in {int(RATE_LIMIT_WINDOW_SECONDS)}s")
        message_id = f"pm_{uuid.uuid4().hex}"
        envelope = {
            "id": message_id, "from_session_id": from_sid, "from_title": str(from_title or ""),
            "from_cwd": str(from_cwd or ""), "to_session_id": to_sid, "body": body, "created_at": now,
        }
        _write(box / "queue" / f"{time.time_ns():020d}-{message_id}.json", envelope)
        rate = {k: [t for t in v if isinstance(t, (int, float)) and now - t < RATE_LIMIT_WINDOW_SECONDS]
                for k, v in rate.items() if isinstance(v, list)}
        rate[from_sid] = recent + [now]
        _write(rate_path, {k: v for k, v in rate.items() if v})
    return {"status": "queued", "message_id": message_id}


def has_pending(session_id: str) -> bool:
    """Cheap, lock-free check for queued messages."""
    try:
        queue = _session_dir(session_id, create=False) / "queue"
        with os.scandir(queue) as it:
            return any(e.name.endswith(".json") for e in it)
    except (OSError, ValueError):
        return False


def claim_next(session_id: str) -> dict | None:
    """Claim the oldest queued message (FIFO). Each message is returned at most once, ever."""
    if not has_pending(session_id):
        return None
    with _locked(session_id) as box:
        for name in sorted(n for n in os.listdir(box / "queue") if n.endswith(".json")):
            src = box / "queue" / name
            envelope = _read(src)
            mid = (envelope or {}).get("id")
            if not envelope or not isinstance(mid, str) or not _MESSAGE_ID_RE.fullmatch(mid):
                # Quarantine instead of wedging the queue on a malformed record.
                os.replace(src, box / "queue" / f"{name}.bad")
                continue
            os.replace(src, box / "claimed" / f"{mid}.json")
            fsync_directory(box / "queue")
            fsync_directory(box / "claimed")
            return {k: envelope.get(k) for k in (
                "id", "from_session_id", "from_title", "from_cwd", "to_session_id", "body", "created_at")}
    return None


def complete(session_id: str, message_id: str, outcome: str, detail: str = "") -> None:
    """Record the terminal outcome of a claimed message and drop the claim."""
    mid = _mid(message_id)
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {sorted(OUTCOMES)}")
    with _locked(session_id) as box:
        if (box / "done" / f"{mid}.json").exists():
            return  # receipts are permanent; first outcome wins
        _write(box / "done" / f"{mid}.json", {
            "id": mid, "to_session_id": session_id, "outcome": outcome,
            "detail": str(detail or "")[:500], "completed_at": time.time(),
        })
        try:
            (box / "claimed" / f"{mid}.json").unlink()
        except FileNotFoundError:
            pass


def get_receipt(to_session_id: str, message_id: str) -> dict | None:
    try:
        return _read(_session_dir(to_session_id, create=False) / "done" / f"{_mid(message_id)}.json")
    except ValueError:
        return None


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def gc(max_age_seconds: float = 86400) -> int:
    """Remove receipts/claims/queued messages older than ``max_age_seconds`` and inbox dirs of
    sessions with no live presence and nothing left inside. Returns the number of removed entries."""
    root = _root()
    if not root.is_dir():
        return 0
    removed = 0
    cutoff = time.time() - max_age_seconds
    for box in sorted(p for p in root.iterdir() if p.is_dir()):
        try:
            sid = _sid(box.name)
        except ValueError:
            continue
        with _locked(sid):
            for sub in _SUBDIRS:
                for entry in (box / sub).iterdir():
                    if entry.is_file() and _mtime(entry) < cutoff:
                        entry.unlink(missing_ok=True)
                        removed += 1
            presence_path = box / "presence.json"
            if presence_path.exists() and not _presence_is_live(_read(presence_path)) \
                    and _mtime(presence_path) < cutoff:
                presence_path.unlink(missing_ok=True)
                removed += 1
            empty = not presence_path.exists() and not any(any((box / sub).iterdir()) for sub in _SUBDIRS)
        if empty:
            shutil.rmtree(box, ignore_errors=True)
            removed += 1
    return removed
