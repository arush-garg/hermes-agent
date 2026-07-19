"""
Periodic monitor tools for Hermes Agent.

Exposes ``add_monitor`` and ``delete_monitors`` — tools that let the agent
inject periodic steer messages into its own conversation.  Each tick fires
``agent.steer(text)`` so the message lands on the next tool result without
interrupting the current tool call.

Monitor state is in-memory, keyed by session_id, and is automatically cleaned
up when the agent closes (via ``register_agent`` / ``unregister_agent`` called
from ``agent_init.py`` and ``run_agent.close()``).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import weakref
from dataclasses import dataclass
from typing import Dict, Optional

from tools.registry import registry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_monitors: Dict[str, "MonitorState"] = {}
_agent_registry: Dict[str, weakref.ref] = {}  # session_id -> weak agent ref


@dataclass
class MonitorState:
    """State for a single periodic monitor."""

    session_id: str
    message: str
    interval_seconds: float
    ticks_remaining: int  # -1 = infinite
    tick_count: int = 0
    timer: Optional[threading.Timer] = None
    lock: threading.Lock = threading.Lock()


# ---------------------------------------------------------------------------
# Agent registry  (called from agent/agent_init.py and run_agent.py)
# ---------------------------------------------------------------------------


def register_agent(session_id: str, agent) -> None:
    """Register a live agent so monitor ticks can reach it.

    Called during agent initialisation; the agent reference is held as a
    weakref so monitor state never prevents garbage collection.
    """
    if not session_id:
        return
    with _lock:
        _agent_registry[session_id] = weakref.ref(agent)


def unregister_agent(session_id: str) -> None:
    """Unregister an agent and cancel all its active monitors.

    Called from ``AIAgent.close()``.
    """
    if not session_id:
        return
    with _lock:
        _agent_registry.pop(session_id, None)
    _cleanup_session(session_id)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _cleanup_session(session_id: str) -> None:
    """Cancel any active timer for *session_id* and remove its monitor(s)."""
    with _lock:
        state = _monitors.pop(session_id, None)
    if state is not None:
        with state.lock:
            if state.timer is not None:
                state.timer.cancel()
                state.timer = None
        logger.info("Monitor cleared for session %s", session_id[:8])


def _tick_cb(session_id: str) -> None:
    """Timer callback — inject the monitor message into the agent via steer."""
    # Resolve the agent reference (may have been GC'd).
    with _lock:
        ref = _agent_registry.get(session_id)
    if ref is None:
        logger.debug("Monitor tick: no agent registered for %s, cleaning up", session_id[:8])
        _cleanup_session(session_id)
        return

    agent = ref()
    if agent is None:
        logger.debug("Monitor tick: agent for %s has expired, cleaning up", session_id[:8])
        _cleanup_session(session_id)
        return

    # Read state under the per-monitor lock, but call agent.steer() outside
    # it to avoid any potential ordering issues with steer's own internal lock.
    with _lock:
        state = _monitors.get(session_id)
    if state is None:
        return

    with state.lock:
        msg = state.message
        count = state.tick_count + 1
        ticks_left = state.ticks_remaining
        state.tick_count = count
        if ticks_left > 0:
            state.ticks_remaining = ticks_left - 1
            ticks_left = state.ticks_remaining
        alive = ticks_left > 0 or ticks_left == -1

    # Inject the steer message (outside the monitor lock).
    try:
        steer_text = f"[Monitor #{count}] {msg}"
        agent.steer(steer_text)
        logger.info("Monitor tick #%d for session %s", count, session_id[:8])
    except Exception:
        logger.exception("Monitor tick #%d failed for session %s", count, session_id[:8])
        alive = False

    # Schedule next tick or clean up.
    if alive:
        with state.lock:
            state.timer = threading.Timer(state.interval_seconds, _tick_cb, args=[session_id])
            state.timer.daemon = True
            state.timer.start()
    else:
        _cleanup_session(session_id)


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

    # Cancel any existing monitor for this session first.
    _cleanup_session(session_id)

    state = MonitorState(
        session_id=session_id,
        message=message,
        interval_seconds=interval,
        ticks_remaining=ticks,
    )

    with _lock:
        _monitors[session_id] = state

    with state.lock:
        state.timer = threading.Timer(interval, _tick_cb, args=[session_id])
        state.timer.daemon = True
        state.timer.start()

    ticks_display = "infinite" if ticks == -1 else str(ticks)
    logger.info(
        "Monitor started for session %s: every %ss, %s tick(s): %r",
        session_id[:8], interval, ticks_display, message[:60],
    )

    return json.dumps({
        "success": True,
        "monitor_message": message,
        "interval_seconds": interval,
        "ticks": ticks,
    })


def handle_delete_monitors(args: dict, **kw) -> str:
    """Cancel all active monitors for the current session."""
    session_id = kw.get("session_id") or os.environ.get("HERMES_SESSION_ID", "")
    if not session_id:
        return json.dumps({"error": "No session_id available"})

    _cleanup_session(session_id)

    return json.dumps({"success": True, "cleared": True})


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
