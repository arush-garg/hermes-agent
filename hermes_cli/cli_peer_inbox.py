"""Classic-CLI receiver for cross-session peer messages (tools/peer_inbox.py).

A daemon thread heartbeats this session's presence every second and delivers queued peer
messages the same way /steer lands: while a turn runs, ``agent.steer(body, sender=...)``
(PEER-framed, no user authority); while idle, the framed message becomes the next turn via
``_pending_input`` (wrapped as a literal seeded query, so it is never parsed as a command).
"""

from __future__ import annotations

import atexit
import inspect
import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

PEER_INBOX_POLL_SECONDS = 1.0
_WARN_INTERVAL_S = 60.0


def peer_messaging_settings(config: Any) -> tuple[bool, str]:
    """``(enabled, inbound)`` from the ``peer_messaging`` config section (defaults: enabled, accept)."""
    section = config.get("peer_messaging") if isinstance(config, dict) else None
    section = section if isinstance(section, dict) else {}
    enabled = section.get("enabled", True)
    if not isinstance(enabled, bool):
        enabled = str(enabled).strip().lower() not in {"0", "false", "no", "off"}
    inbound = "refuse" if str(section.get("inbound") or "accept").strip().lower() == "refuse" else "accept"
    return enabled, inbound


def _accepts_sender(agent: Any) -> bool:
    steer = getattr(agent, "steer", None)
    if not callable(steer):
        return False
    try:
        params = inspect.signature(steer).parameters
    except (TypeError, ValueError):
        return False
    return "sender" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


class CliPeerInboxPoller:
    """Per-process poller bound to a ``HermesCLI`` instance (reads its live attributes every tick)."""

    def __init__(self, cli: Any, *, inbound: str = "accept", interval: float = PEER_INBOX_POLL_SECONDS):
        self.cli = cli
        self.inbound = inbound
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._presence_id = ""
        self._warned_at = -_WARN_INTERVAL_S
        self._suppressed = 0

    # -- lifecycle -------------------------------------------------------------------------
    def start(self) -> "CliPeerInboxPoller":
        self._thread = threading.Thread(target=self._run, daemon=True, name="cli-peer-inbox")
        self._thread.start()
        atexit.register(self.stop)
        return self

    def stop(self) -> None:
        self._stop.set()
        self._clear()

    def _run(self) -> None:
        try:
            while not self._stop.is_set() and not getattr(self.cli, "_should_exit", False):
                self._guarded_tick()
                self._stop.wait(self.interval)
        finally:
            self._clear()

    def _clear(self) -> None:
        old, self._presence_id = self._presence_id, ""
        if old:
            from tools import peer_inbox
            try:
                peer_inbox.clear_presence(old)
            except Exception:
                logger.debug("peer presence clear failed", exc_info=True)

    def _guarded_tick(self) -> None:
        import time
        try:
            self.tick()
            self._suppressed = 0
        except Exception:
            now = time.monotonic()
            if now - self._warned_at < _WARN_INTERVAL_S:
                self._suppressed += 1
                logger.debug("CLI peer inbox poll failed (repeat)", exc_info=True)
                return
            logger.warning("CLI peer inbox poll failed (%d repeat(s) suppressed)", self._suppressed, exc_info=True)
            self._warned_at, self._suppressed = now, 0

    # -- one pass --------------------------------------------------------------------------
    def _title(self, session_id: str) -> str:
        title = getattr(self.cli, "_pending_title", None) or ""
        db = getattr(self.cli, "_session_db", None)
        if db is not None and not title:
            try:
                title = db.get_session_title(session_id) or ""
            except Exception:
                title = ""
        return str(title)

    def tick(self) -> str | None:
        """Heartbeat presence, then deliver at most one message. Returns the receipt outcome or None."""
        from tools import peer_inbox

        cli = self.cli
        session_id = str(getattr(cli, "session_id", "") or "")
        if self._presence_id and self._presence_id != session_id:
            self._clear()  # /new, /resume or compression rotated the id
        if not session_id:
            return None
        busy = bool(getattr(cli, "_agent_running", False))
        peer_inbox.write_presence(session_id, pid=os.getpid(), surface="cli", busy=busy, inbound=self.inbound,
                                  title=self._title(session_id), cwd=os.getcwd())
        self._presence_id = session_id
        if not peer_inbox.has_pending(session_id):
            return None
        agent = getattr(cli, "agent", None)
        if busy:
            if agent is None or not _accepts_sender(agent):
                return None  # turn still building its agent: leave it queued, retry next tick
            env = peer_inbox.claim_next(session_id)
            if env is None:
                return None
            try:
                accepted = agent.steer(env["body"], sender={
                    "session_id": env.get("from_session_id") or "", "title": env.get("from_title") or ""})
            except Exception as exc:
                peer_inbox.complete(session_id, env["id"], "failed", f"steer failed: {exc}")
                return "failed"
            if not accepted:
                peer_inbox.complete(session_id, env["id"], "failed", "receiver rejected the message")
                return "failed"
            peer_inbox.complete(session_id, env["id"], "delivered_steer")
            return "delivered_steer"
        pending = getattr(cli, "_pending_input", None)
        if pending is None:
            return None
        env = peer_inbox.claim_next(session_id)
        if env is None:
            return None
        from agent.prompt_builder import format_peer_message
        framed = format_peer_message(from_session_id=env.get("from_session_id") or "",
                                     from_title=env.get("from_title") or "", body=env["body"])
        try:
            from cli import _SeededQueryMessage  # literal: no slash / ! / file-drop parsing
            item: Any = _SeededQueryMessage(framed)
        except Exception:
            item = framed
        try:
            pending.put(item)
        except Exception as exc:
            peer_inbox.complete(session_id, env["id"], "failed", f"enqueue failed: {exc}")
            return "failed"
        peer_inbox.complete(session_id, env["id"], "delivered_turn")
        return "delivered_turn"


def start_cli_peer_inbox_poller(cli: Any) -> CliPeerInboxPoller | None:
    """Start the poller unless ``peer_messaging.enabled`` is false. Never raises."""
    try:
        enabled, inbound = peer_messaging_settings(getattr(cli, "config", None))
        if not enabled:
            return None
        return CliPeerInboxPoller(cli, inbound=inbound).start()
    except Exception:
        logger.debug("CLI peer inbox poller not started", exc_info=True)
        return None
