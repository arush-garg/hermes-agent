"""Session waits — one-shot deferred user messages (``/wait 2h continue working on this``).

Sibling of ``heartbeat.py``: same duration grammar (``parse_duration``) and the same idle-injection drivers
(CLI watchdog, gateway heartbeat poller, TUI session-owner tick), so the same invariants hold — a due wait is
injected verbatim as a plain user message into an idle session with an empty input queue (no system-prompt
mutation, strict role alternation), and a real user message always wins. Session-scoped and in-process like
heartbeats; ``hermes cron`` owns durable scheduling."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from hermes_cli.heartbeat import format_interval, parse_duration

logger = logging.getLogger(__name__)

_META_PREFIX = "wait:"
_MAX_TOMBSTONES = 32

WAIT_USAGE = (
    "Usage: /wait <duration> <message>   (e.g. /wait 2h continue working on this)\n"
    "Also: /wait list | cancel [<id>|all]"
)


@dataclass
class PendingWait:
    id: int
    prompt: str
    due_at: float
    created_at: float = 0.0


@dataclass
class WaitState:
    waits: list[PendingWait] = field(default_factory=list)
    next_id: int = 1
    # A claimed wait is out of ``waits`` until its turn starts or is refunded, so a cancel landing in that window
    # cannot remove it; these record the cancel instead and refunds check them. ``cancelled_below``: every id under
    # it was cancelled (``cancel all``). ``cancelled_ids``: single cancels of ids that were not pending.
    cancelled_below: int = 0
    cancelled_ids: list[int] = field(default_factory=list)

    def is_cancelled(self, wait_id: int) -> bool:
        return wait_id < self.cancelled_below or wait_id in self.cancelled_ids

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "WaitState":
        data = json.loads(raw)
        waits = [PendingWait(id=int(w["id"]), prompt=str(w["prompt"]), due_at=float(w["due_at"]),
                             created_at=float(w.get("created_at") or 0.0)) for w in data.get("waits") or ()]
        return cls(waits=waits, next_id=int(data.get("next_id") or 1),
                   cancelled_below=int(data.get("cancelled_below") or 0),
                   cancelled_ids=[int(i) for i in data.get("cancelled_ids") or ()])


def _get_session_db() -> Optional[Any]:
    """The goals module's per-HERMES_HOME cached SessionDB (one shared connection, as for heartbeats)."""
    try:
        from hermes_cli.goals import _get_session_db as _goals_db
        return _goals_db()
    except Exception as exc:  # pragma: no cover
        logger.debug("WaitManager: SessionDB bootstrap failed (%s)", exc)
        return None


def load_waits(session_id: str) -> WaitState:
    db = _get_session_db() if session_id else None
    if db is None:
        return WaitState()
    try:
        raw = db.get_meta(_META_PREFIX + session_id)
        return WaitState.from_json(raw) if raw else WaitState()
    except Exception as exc:
        logger.warning("WaitManager: could not load waits for %s: %s", session_id, exc)
        return WaitState()


def save_waits(session_id: str, state: WaitState) -> None:
    if not session_id:
        return
    db = _get_session_db()
    if db is None:
        from hermes_cli.goals import _warn_dropped_write
        _warn_dropped_write("WaitManager", "wait", session_id)
        return
    try:
        db.set_meta(_META_PREFIX + session_id, state.to_json())
    except Exception as exc:
        logger.debug("WaitManager: set_meta failed: %s", exc)


def store_has_pending_wait(db: Any) -> bool:
    """True when *db* holds a ``wait:*`` row with a pending wait — or one that cannot be parsed (unknown, so the
    caller keeps its full sweep). Read errors propagate, as in ``store_has_active_heartbeat``."""
    for _key, raw in db.list_meta_prefix(_META_PREFIX):
        try:
            if raw and WaitState.from_json(raw).waits:
                return True
        except Exception:
            return True
    return False


def format_remaining(seconds: float) -> str:
    """Countdown for display: the two largest non-zero units (``7193`` → ``1h 59m``)."""
    seconds = max(0, int(seconds))
    parts = []
    for unit, suffix in ((86400, "d"), (3600, "h"), (60, "m"), (1, "s")):
        if seconds >= unit or (unit == 1 and not parts):
            parts.append(f"{seconds // unit}{suffix}")
            seconds %= unit
    return " ".join(parts[:2])


class WaitManager:
    """Per-session pending waits; the driver surface mirrors ``HeartbeatManager`` (``is_active`` /
    ``is_due`` / ``due_prompt`` / ``abandon_fire``) so the heartbeat drivers poll both.

    Every mutation re-reads the store first: the command may run in one process (a TUI slash worker) while
    another (the session owner) claims due waits, and a cached list would drop the other side's write.
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._state = load_waits(session_id)
        self._last_claim: Optional[PendingWait] = None  # claimed by the last due_prompt, refundable

    def _reload(self) -> WaitState:
        self._state = load_waits(self.session_id)
        return self._state

    @property
    def pending(self) -> list[PendingWait]:
        return sorted(self._state.waits, key=lambda w: (w.due_at, w.id))

    def is_active(self) -> bool:
        return bool(self._state.waits)

    def is_due(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        return any(w.due_at <= now for w in self._state.waits)

    def add(self, prompt: str, delay_seconds: int, now: Optional[float] = None) -> PendingWait:
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("wait message is empty")
        if int(delay_seconds) <= 0:
            raise ValueError("duration must be positive")
        now = time.time() if now is None else now
        state = self._reload()
        wait = PendingWait(id=state.next_id, prompt=prompt, due_at=now + int(delay_seconds), created_at=now)
        state.waits.append(wait)
        state.next_id += 1
        save_waits(self.session_id, state)
        return wait

    def cancel(self, wait_id: Optional[int] = None) -> int:
        """Cancel one wait by id, or all when *wait_id* is None; returns how many pending waits were removed.
        Also covers a wait claimed but not yet started, so a refund cannot resurrect it."""
        state = self._reload()
        kept = [w for w in state.waits if wait_id is not None and w.id != wait_id]
        removed = len(state.waits) - len(kept)
        state.waits = kept
        if wait_id is None:
            state.cancelled_below = state.next_id
        elif not removed and 0 < wait_id < state.next_id:
            state.cancelled_ids = [*state.cancelled_ids, wait_id][-_MAX_TOMBSTONES:]
        save_waits(self.session_id, state)
        return removed

    def due_prompt(self, now: Optional[float] = None) -> Optional[str]:
        """Claim and return the earliest due wait's message, else None. The claim is persisted before the turn
        runs so overlapping drivers never double-send it; :meth:`abandon_fire` refunds a turn that never started."""
        now = time.time() if now is None else now
        state = self._reload()
        due = [w for w in state.waits if w.due_at <= now]
        if not due:
            return None
        wait = min(due, key=lambda w: (w.due_at, w.id))
        state.waits.remove(wait)
        save_waits(self.session_id, state)
        self._last_claim = wait
        return wait.prompt

    def abandon_fire(self) -> bool:
        """Put back the wait claimed by the last :meth:`due_prompt` whose turn never started, so it stays due for
        the next idle poll. Skipped (False) when a cancel landed in between — the user's cancel wins."""
        wait, self._last_claim = self._last_claim, None
        if wait is None:
            return False
        state = self._reload()
        if state.is_cancelled(wait.id) or any(w.id == wait.id for w in state.waits):
            return False
        state.waits.append(wait)
        save_waits(self.session_id, state)
        return True

    def status_line(self, now: Optional[float] = None) -> str:
        now = time.time() if now is None else now
        pending = self.pending
        if not pending:
            return "No pending waits. Set one with /wait <duration> <message>."
        lines = [f"⏳ {len(pending)} pending wait{'s' if len(pending) != 1 else ''}:"]
        lines += [f"  #{w.id} in ~{format_remaining(w.due_at - now)}: {w.prompt}" for w in pending]
        return "\n".join(lines)


def run_wait_command(mgr: WaitManager, args: str) -> tuple[str, bool]:
    """The whole ``/wait`` grammar for every surface: ``(reply, armed)``; *armed* tells the caller a new wait
    was stored and its driver (CLI watchdog / gateway poller) must be running."""
    args = (args or "").strip()
    tokens = args.split(None, 1)
    head = tokens[0].lower() if tokens else ""
    rest = tokens[1].strip() if len(tokens) > 1 else ""
    if head in {"", "list", "status"} and not rest:
        return mgr.status_line(), False
    if head in {"cancel", "clear", "stop", "off"}:
        target = rest.lstrip("#")
        if target.lower() in {"", "all"}:
            removed = mgr.cancel()
            return (f"✓ Cancelled {removed} pending wait{'s' if removed != 1 else ''}." if removed
                    else "No pending waits."), False
        if not target.isdigit():
            return "Usage: /wait cancel [<id>|all]", False
        return (f"✓ Cancelled wait #{target}." if mgr.cancel(int(target)) else f"No pending wait #{target}."), False

    delay = parse_duration(tokens[0]) if tokens else None
    if delay is None:
        return WAIT_USAGE, False
    if delay <= 0:
        return "Duration must be positive.", False
    if not rest:
        return "Usage: /wait <duration> <message> — the message is required.", False
    if rest.startswith("/"):
        return ("/wait sends a chat message, not a slash command — for a recurring command use /loop, "
                "for a durable schedule use `hermes cron`."), False
    try:
        wait = mgr.add(rest, delay)
    except ValueError as exc:
        return f"Invalid wait: {exc}", False
    return (f"⏳ Wait #{wait.id} set — sends in {format_interval(delay)}: {wait.prompt}\n"
            "Arrives as your next message once the session is idle. /wait list | cancel to manage; "
            "lives only while Hermes runs — use `hermes cron` for durable schedules."), True


def migrate_waits_to_session(old_session_id: str, new_session_id: str) -> bool:
    """Carry pending waits across a compression session rotation (merge into child, clear parent, never raise)."""
    if not old_session_id or not new_session_id or old_session_id == new_session_id:
        return False
    try:
        old = load_waits(old_session_id)
        if not old.waits:
            return False
        new = load_waits(new_session_id)
        for wait in old.waits:
            new.waits.append(PendingWait(id=new.next_id, prompt=wait.prompt, due_at=wait.due_at,
                                         created_at=wait.created_at))
            new.next_id += 1
        save_waits(new_session_id, new)
        old.waits = []
        old.cancelled_below = old.next_id
        save_waits(old_session_id, old)
        return True
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("WaitManager: migration failed: %s", exc)
        return False


__all__ = [
    "PendingWait", "WaitState", "WaitManager", "WAIT_USAGE", "load_waits", "save_waits",
    "store_has_pending_wait", "format_remaining", "run_wait_command", "migrate_waits_to_session",
]
