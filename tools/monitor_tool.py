"""
Periodic monitor tools for Hermes Agent.

Exposes ``add_monitor`` and ``delete_monitors`` — tools that let the agent
inject periodic steer messages into its own conversation.  Each tick fires
``agent.steer(text)`` so the message lands on the next tool result without
interrupting the current tool call.

Monitor state is in-memory, keyed by unique UUID, and persisted to
``~/.hermes/monitors.json`` so monitors survive agent restarts.  Multiple
concurrent monitors per session are supported.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
import weakref
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional

from tools.registry import registry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
_MONITORS_FILE = HERMES_HOME / "monitors.json"

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_DELIVER_MODES = ("local", "discord", "disposable")
# Sentinel disposable subagents emit when their check is uneventful so the
# parent is never disturbed by a monitor that has nothing to report.
_NO_ACTION_TOKEN = "[NO_ACTION]"

_lock = threading.Lock()
_monitors: Dict[str, "MonitorState"] = {}  # uuid -> MonitorState
_agent_registry: Dict[str, weakref.ref] = {}  # session_id -> weak agent ref


@dataclass
class MonitorState:
    """State for a single periodic monitor."""

    monitor_id: str
    session_id: str
    message: str
    interval_seconds: float
    ticks_remaining: int  # -1 = infinite
    tick_count: int = 0
    timer: Optional[threading.Timer] = field(default=None, repr=False)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    enabled: bool = True
    deliver: str = "local"  # "local" | "discord" | "disposable"
    subagent_context: Optional[str] = None  # system prompt for disposable subagent
    # Runtime-only — never persisted to monitors.json.
    _live_subagent_id: Optional[str] = field(default=None, repr=False)
    _queued_spawn: bool = field(default=False, repr=False)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _load_persisted_monitors() -> List[dict]:
    """Load persisted monitor definitions from disk."""
    if not _MONITORS_FILE.exists():
        return []
    try:
        data = json.loads(_MONITORS_FILE.read_text())
        return data.get("monitors", [])
    except Exception:
        logger.exception("Failed to load persisted monitors from %s", _MONITORS_FILE)
        return []


def _save_persisted_monitors() -> None:
    """Save active monitor definitions to disk (exclude running timers)."""
    with _lock:
        active = [
            {
                "monitor_id": m.monitor_id,
                "session_id": m.session_id,
                "message": m.message,
                "interval_seconds": m.interval_seconds,
                "ticks_remaining": m.ticks_remaining,
                "tick_count": m.tick_count,
                "enabled": m.enabled,
                "deliver": m.deliver,
                "subagent_context": m.subagent_context,
            }
            for m in _monitors.values()
            if m.enabled and m.timer is None
        ]
    try:
        _MONITORS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _MONITORS_FILE.write_text(json.dumps({"monitors": active}, indent=2))
    except Exception:
        logger.exception("Failed to save persisted monitors")


def restore_monitors(session_id: str) -> int:
    """Restore persisted monitors for *session_id* and restart their timers.

    Returns the number of monitors restored.
    """
    persisted = _load_persisted_monitors()
    restored = 0
    for entry in persisted:
        # Match by prefix so monitors survive session restarts
        # (stored session_id may be date-only prefix like 20260826
        # while register_agent gets full ID like 20260826_220517_xxx)
        if not entry.get("session_id", "").startswith(session_id[:8]):
            continue
        if not entry.get("enabled", True):
            continue
        monitor_id = entry["monitor_id"]
        # Skip if already active (re-start from register_agent).
        with _lock:
            if monitor_id in _monitors:
                continue
        state = MonitorState(
            monitor_id=monitor_id,
            session_id=session_id,
            message=entry["message"],
            interval_seconds=float(entry["interval_seconds"]),
            ticks_remaining=int(entry.get("ticks_remaining", -1)),
            tick_count=int(entry.get("tick_count", 0)),
            deliver=entry.get("deliver") or "local",
            subagent_context=entry.get("subagent_context"),
        )
        with _lock:
            _monitors[monitor_id] = state
        _start_timer(state)
        restored += 1
        logger.info("Restored monitor %s for session %s", monitor_id[:8], session_id[:8])
    return restored


# ---------------------------------------------------------------------------
# Agent registry  (called from agent/agent_init.py and run_agent.py)
# ---------------------------------------------------------------------------


def register_agent(session_id: str, agent) -> None:
    """Register a live agent so monitor ticks can reach it.

    Called during agent initialisation; the agent reference is held as a
    weakref so monitor state never prevents garbage collection.
    Also restores any persisted monitors for this session.
    """
    if not session_id:
        return
    with _lock:
        _agent_registry[session_id] = weakref.ref(agent)
    count = restore_monitors(session_id)
    if count:
        logger.info("Restored %d monitor(s) for session %s", count, session_id[:8])


def unregister_agent(session_id: str) -> None:
    """Unregister an agent and cancel all its active monitors.

    Called from ``AIAgent.close()``.
    """
    if not session_id:
        return
    with _lock:
        _agent_registry.pop(session_id, None)
    _cleanup_session(session_id)
    _save_persisted_monitors()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _cleanup_session(session_id: str) -> None:
    """Cancel all active timers for monitors belonging to *session_id*."""
    with _lock:
        to_remove = [
            mid for mid, m in _monitors.items() if m.session_id == session_id
        ]
        for mid in to_remove:
            _monitors.pop(mid, None)
    for mid in to_remove:
        state = _monitors.get(mid)  # already popped; use local ref
        # Re-fetch from the dict before pop to cancel timer
        pass
    # Cancel timers (we popped above, so re-acquire to get state)
    with _lock:
        for mid, state in list(_monitors.items()):
            if state.session_id == session_id:
                with state.lock:
                    if state.timer is not None:
                        state.timer.cancel()
                        state.timer = None
                _monitors.pop(mid, None)
    if to_remove:
        logger.info("Cleared %d monitor(s) for session %s", len(to_remove), session_id[:8])


def _start_timer(state: MonitorState) -> None:
    """Start (or restart) the timer for a monitor state."""
    with state.lock:
        if state.timer is not None:
            state.timer.cancel()
        state.timer = threading.Timer(
            state.interval_seconds, _tick_cb, args=[state.monitor_id]
        )
        state.timer.daemon = True
        state.timer.start()


def _spawn_disposable_subagent(state: MonitorState, parent_agent, tick: int) -> None:
    """Run one disposable subagent for a monitor tick; steer parent if actionable.

    Executes on a daemon thread.  The subagent is asked to emit ``[NO_ACTION]``
    when nothing needs attention, so quiet ticks never reach the parent.  On
    completion the live-subagent slot is cleared and any queued spawn drained.
    """
    from agent.subagent_lifecycle import (
        SubagentLifecycleService,
        SubagentLaunchRequest,
    )

    monitor_id = state.monitor_id
    session_id = state.session_id

    goal = (
        f"{state.message}\n\n"
        "REPORTING RULE: If your check finds something actionable (threshold "
        "breached, alert condition met, action required), output your findings "
        "as plain text.  If everything is normal and no action is required, "
        f"output exactly the token: {_NO_ACTION_TOKEN}"
    )

    try:
        service = SubagentLifecycleService(lambda: parent_agent)
        handle = service.launch(
            SubagentLaunchRequest(
                goal=goal,
                context=state.subagent_context or None,
                role="leaf",
                model=None,
                allowed_toolsets=None,
                # Correlation ids are retained for an hour after a child ends,
                # and duplicates are rejected — so make each tick unique.
                correlation_id=f"monitor-{monitor_id[:8]}-{tick}-{uuid.uuid4().hex[:6]}",
                timeout_seconds=None,
            )
        )

        logger.info(
            "Disposable subagent launched [%s] tick #%d for session %s",
            monitor_id[:8], tick, session_id[:8],
        )

        service.wait(handle)
        result = service.result(handle)
        report = (getattr(result, "summary", None) or "").strip()

        if report and report != _NO_ACTION_TOKEN:
            try:
                parent_agent.steer(f"[Monitor {monitor_id[:8]}] {report}")
                logger.info("Disposable subagent reported back [%s]", monitor_id[:8])
            except Exception:
                logger.exception(
                    "Failed to steer parent from disposable subagent [%s]", monitor_id[:8]
                )
        else:
            logger.debug("Disposable subagent [%s]: no action", monitor_id[:8])

    except Exception:
        logger.exception("Disposable subagent failed [%s]", monitor_id[:8])
    finally:
        with state.lock:
            state._live_subagent_id = None
            queued = state._queued_spawn
            state._queued_spawn = False

        if queued and state.enabled:
            logger.info("Draining queued spawn for monitor [%s]", monitor_id[:8])
            _launch_disposable_for_monitor(state)


def _launch_disposable_for_monitor(state: MonitorState) -> None:
    """Resolve the parent agent and start a disposable subagent thread.

    If a subagent for this monitor is still running the tick is queued instead
    (depth-1: the most recent tick wins, older queued ticks collapse into it).
    """
    with _lock:
        ref = _agent_registry.get(state.session_id)
    if ref is None:
        logger.warning(
            "No agent ref for session %s, skipping disposable spawn",
            state.session_id[:8],
        )
        return

    parent_agent = ref()
    if parent_agent is None:
        logger.warning(
            "Parent agent GC'd for session %s, disabling monitor [%s]",
            state.session_id[:8], state.monitor_id[:8],
        )
        with _lock:
            state.enabled = False
        return

    with state.lock:
        if state._live_subagent_id is not None:
            state._queued_spawn = True
            logger.info(
                "Monitor [%s]: previous subagent still live, queued spawn",
                state.monitor_id[:8],
            )
            return
        state._live_subagent_id = f"pending-{state.monitor_id[:8]}"
        tick = state.tick_count

    threading.Thread(
        target=_spawn_disposable_subagent,
        args=(state, parent_agent, tick),
        daemon=True,
        name=f"hermes-disposable-{state.monitor_id[:8]}",
    ).start()


def _tick_cb(monitor_id: str) -> None:
    """Timer callback — inject the monitor message into the agent via steer."""
    with _lock:
        state = _monitors.get(monitor_id)
        ref = _agent_registry.get(state.session_id) if state is not None else None
    if state is None or not state.enabled:
        return

    agent = None
    if ref is not None:
        agent = ref()

    if agent is None:
        logger.debug("Monitor tick: no agent for %s, disabling", monitor_id[:8])
        with _lock:
            if monitor_id in _monitors:
                _monitors[monitor_id].enabled = False
        return

    # Read state under the per-monitor lock, but call agent.steer() outside
    # it to avoid any potential ordering issues with steer's own internal lock.
    with state.lock:
        msg = state.message
        count = state.tick_count + 1
        ticks_left = state.ticks_remaining
        state.tick_count = count
        if ticks_left > 0:
            state.ticks_remaining = ticks_left - 1
            ticks_left = state.ticks_remaining
        alive = ticks_left > 0 or ticks_left == -1

    if state.deliver == "disposable":
        _launch_disposable_for_monitor(state)
        if alive:
            _start_timer(state)
        else:
            with _lock:
                _monitors.pop(monitor_id, None)
            logger.info("Monitor [%s] exhausted ticks, removed", monitor_id[:8])
        return

    # Inject the steer message (outside the monitor lock).
    try:
        steer_text = f"[Monitor #{count}] {msg}"
        agent.steer(steer_text)
        logger.info("Monitor tick #%d [%s] for session %s", count, monitor_id[:8], state.session_id[:8])
    except Exception:
        logger.exception("Monitor tick #%d failed [%s]", count, monitor_id[:8])
        alive = False

    # Schedule next tick or clean up.
    if alive:
        _start_timer(state)
    else:
        with _lock:
            _monitors.pop(monitor_id, None)
        logger.info("Monitor [%s] exhausted ticks, removed", monitor_id[:8])


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def handle_add_monitor(args: dict, **kw) -> str:
    """Create a new periodic monitor for the current session."""
    session_id = kw.get("session_id") or os.environ.get("HERMES_SESSION_ID", "")
    if not session_id:
        return json.dumps({"error": "No session_id available"})

    message = (args.get("message") or "").strip()
    if not message:
        return json.dumps({"error": "message is required"})

    interval = args.get("interval_seconds", 0)
    try:
        interval = float(interval)
    except (TypeError, ValueError):
        return json.dumps({"error": "interval_seconds must be a number"})
    if interval < 1:
        return json.dumps({"error": "interval_seconds must be >= 1"})

    ticks = args.get("ticks", -1)
    if ticks is None:
        ticks = -1
    try:
        ticks = int(ticks)
    except (TypeError, ValueError):
        return json.dumps({"error": "ticks must be an integer"})
    if ticks == 0 or ticks < -1:
        return json.dumps({"error": "ticks must be -1 (infinite) or >= 1"})

    deliver = args.get("deliver") or "local"
    if deliver not in _DELIVER_MODES:
        return json.dumps({
            "error": f"deliver must be one of {sorted(_DELIVER_MODES)}"
        })

    subagent_context = args.get("subagent_context")
    if subagent_context is not None:
        subagent_context = str(subagent_context).strip() or None

    monitor_id = str(uuid.uuid4())

    state = MonitorState(
        monitor_id=monitor_id,
        session_id=session_id,
        message=message,
        interval_seconds=interval,
        ticks_remaining=ticks,
        deliver=deliver,
        subagent_context=subagent_context,
    )

    with _lock:
        _monitors[monitor_id] = state

    _start_timer(state)

    ticks_display = "infinite" if ticks == -1 else str(ticks)
    logger.info(
        "Monitor started [%s] for session %s: every %ss, %s tick(s): %r",
        monitor_id[:8], session_id[:8], interval, ticks_display, message[:60],
    )

    return json.dumps({
        "success": True,
        "monitor_id": monitor_id,
        "monitor_message": message,
        "interval_seconds": interval,
        "ticks": ticks,
        "deliver": deliver,
        "subagent_context": subagent_context,
    })


def handle_delete_monitors(args: dict, **kw) -> str:
    """Cancel all active monitors for the current session."""
    session_id = kw.get("session_id") or os.environ.get("HERMES_SESSION_ID", "")
    if not session_id:
        return json.dumps({"error": "No session_id available"})

    with _lock:
        count = sum(1 for m in _monitors.values() if m.session_id == session_id)
    _cleanup_session(session_id)
    _save_persisted_monitors()

    return json.dumps({"success": True, "cleared": True, "count": count})


def handle_list_monitors(args: dict, **kw) -> str:
    """List all active monitors for the current session."""
    session_id = kw.get("session_id") or os.environ.get("HERMES_SESSION_ID", "")
    if not session_id:
        return json.dumps({"error": "No session_id available"})

    with _lock:
        active = [
            {
                "monitor_id": m.monitor_id,
                "message": m.message,
                "interval_seconds": m.interval_seconds,
                "ticks_remaining": m.ticks_remaining,
                "tick_count": m.tick_count,
                "enabled": m.enabled,
            }
            for m in _monitors.values()
            if m.session_id == session_id and m.enabled
        ]
    return json.dumps({"success": True, "monitors": active, "count": len(active)})


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

ADD_MONITOR_SCHEMA = {
    "name": "add_monitor",
    "description": (
        "Set up a periodic monitor that injects a message into the "
        "conversation every N seconds.  The message wakes the agent up to "
        "take action.  Use for periodic checks, reminders, or keeping the "
        "agent aware of changing state during long operations."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": (
                    "Message text to inject into the conversation each "
                    "tick.  The agent receives this as if it were user "
                    "input delivered mid-turn."
                ),
            },
            "interval_seconds": {
                "type": "number",
                "description": "Seconds between monitor ticks.  Minimum 1.",
                "minimum": 1,
            },
            "ticks": {
                "type": "integer",
                "description": (
                    "Number of ticks before auto-removal.  -1 = infinite "
                    "(until delete_monitors is called or the session ends)."
                ),
                "default": -1,
            },
            "deliver": {
                "type": "string",
                "enum": ["local", "discord", "disposable"],
                "description": (
                    "Delivery mode. 'local': steer parent every tick. "
                    "'discord': post to Discord. 'disposable': spawn a "
                    "background subagent each tick; subagent reports back "
                    "only if actionable, then exits."
                ),
                "default": "local",
            },
            "subagent_context": {
                "type": "string",
                "description": (
                    "Used when deliver=disposable. Setup context injected "
                    "as the subagent's system prompt — what it's monitoring, "
                    "thresholds, domain facts. Kept stable between ticks. "
                    "Separate from 'message' (the per-tick check instruction)."
                ),
            },
        },
        "required": ["message", "interval_seconds"],
    },
}

DELETE_MONITORS_SCHEMA = {
    "name": "delete_monitors",
    "description": (
        "Cancel all active monitors on the current conversation.  Use "
        "when monitors are no longer needed."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

LIST_MONITORS_SCHEMA = {
    "name": "list_monitors",
    "description": (
        "List all active monitors for the current session, showing "
        "message, interval, ticks remaining, and tick count."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

registry.register(
    name="add_monitor",
    toolset="monitor",
    schema=ADD_MONITOR_SCHEMA,
    handler=handle_add_monitor,
    emoji="⏱️",
)

registry.register(
    name="delete_monitors",
    toolset="monitor",
    schema=DELETE_MONITORS_SCHEMA,
    handler=handle_delete_monitors,
    emoji="🛑",
)

registry.register(
    name="list_monitors",
    toolset="monitor",
    schema=LIST_MONITORS_SCHEMA,
    handler=handle_list_monitors,
    emoji="📋",
)
