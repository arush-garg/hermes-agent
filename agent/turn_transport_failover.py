"""Transport-aware failover bookkeeping for the turn loop.

Two jobs, both keyed on *transport* failures (dns / tcp / timeout, see
``agent/connectivity_probe.py::classify_transport_failure``):

- **Cross-provider circuit breaker.** ``record_transport_failure`` remembers the
  last transport failure (kind, provider host, time). When the next one comes
  from a *different* host with the *same* kind inside ``breaker_window_s``, the
  fallback ladder is not finding a healthy provider — it is walking a dead local
  link. ``breaker_tripped`` says so and the caller parks the turn in the
  connectivity pause instead of activating yet another fallback.
- **Mid-turn primary restore.** Fallback is otherwise sticky for the whole turn
  (``restore_primary_runtime`` runs only at turn start), so a short outage can
  strand a long turn on a weaker model. ``note_fallback_activation`` records when
  the agent left its primary *because of a transport failure*;
  ``maybe_restore_primary_mid_turn`` (called at the API-call boundary, before the
  request is built) cheaply re-dials the primary host every
  ``primary_restore_interval_s`` and restores it once it answers. Fallbacks caused
  by HTTP responses (4xx, quota, auth) never qualify.

State lives on the agent: ``_last_transport_failure`` and ``_transport_fallback``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger("agent.conversation_loop")

_TRANSPORT_KINDS = frozenset({"dns", "tcp", "timeout"})
_DEFAULT_BREAKER_WINDOW_S = 60.0
_DEFAULT_RESTORE_INTERVAL_S = 150.0
_PROBE_PORT = 443


def _cfg_number(agent: Any, key: str, default: float) -> float:
    cfg = getattr(agent, "_connectivity_pause", None)
    value = cfg.get(key, default) if isinstance(cfg, dict) else default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return default
    return float(value)


def record_transport_failure(agent: Any, kind: str, host: str, now: Optional[float] = None) -> Optional[tuple]:
    """Store this transport failure; return the previous record (or None)."""
    previous = getattr(agent, "_last_transport_failure", None)
    agent._last_transport_failure = (kind, host, time.monotonic() if now is None else now)
    return previous if isinstance(previous, tuple) and len(previous) == 3 else None


def clear_transport_failure(agent: Any) -> None:
    """A non-transport outcome (HTTP error / success) breaks the streak."""
    agent._last_transport_failure = None


def breaker_tripped(agent: Any, kind: str, host: str, previous: Optional[tuple], now: Optional[float] = None) -> bool:
    """True when ``previous`` is a same-kind transport failure on a different host
    within ``breaker_window_s`` — two providers dark the same way means local."""
    if previous is None or kind not in _TRANSPORT_KINDS or not host:
        return False
    prev_kind, prev_host, prev_at = previous
    if prev_kind != kind or not prev_host or prev_host == host:
        return False
    window = _cfg_number(agent, "breaker_window_s", _DEFAULT_BREAKER_WINDOW_S)
    current = time.monotonic() if now is None else now
    return window > 0 and (current - prev_at) <= window


def _host_of(base_url: Any) -> str:
    from agent.connectivity_probe import _parse_host_port

    parsed = _parse_host_port(str(base_url or ""), _PROBE_PORT)
    return parsed[0] if parsed else ""


def note_fallback_activation(agent: Any, api_error: Any, was_on_primary: bool, now: Optional[float] = None) -> None:
    """Call right after a successful fallback activation. Leaving the primary for
    a transport failure arms the mid-turn restore; leaving it for anything else
    disarms it. Hops between fallbacks keep the original verdict."""
    if not was_on_primary:
        return
    from agent.connectivity_probe import classify_transport_failure

    kind = classify_transport_failure(api_error)
    rt = getattr(agent, "_primary_runtime", None) or {}
    base_url = str(rt.get("base_url") or "") if isinstance(rt, dict) else ""
    # Only network primaries can be re-dialed; claude-cli:// / moa:// are local.
    host = _host_of(base_url) if base_url.startswith(("http://", "https://")) else ""
    if kind not in _TRANSPORT_KINDS or not host:
        agent._transport_fallback = None
        return
    stamp = time.monotonic() if now is None else now
    agent._transport_fallback = {"kind": kind, "primary_host": host, "since": stamp, "last_probe": stamp}
    logger.info("%sFallback left primary %s on a %s failure; mid-turn restore armed.",
                getattr(agent, "log_prefix", ""), host, kind)


def activate_fallback_tracked(agent: Any, api_error: Any, **kwargs: Any) -> bool:
    """``agent._try_activate_fallback(**kwargs)`` plus ``note_fallback_activation``."""
    was_on_primary = getattr(agent, "_fallback_activated", False) is not True
    if not agent._try_activate_fallback(**kwargs):
        return False
    note_fallback_activation(agent, api_error, was_on_primary)
    return True


def _primary_reachable(agent: Any, host: str) -> bool:
    from agent.connectivity_probe import _tcp_reachable

    timeout = _cfg_number(agent, "probe_timeout_s", 3.0) or 3.0
    return _tcp_reachable(host, _PROBE_PORT, max(timeout, 0.5))


def maybe_restore_primary_mid_turn(agent: Any, now: Optional[float] = None) -> bool:
    """At an API-call boundary: restore the primary if a transport-caused
    fallback is active, the restore interval elapsed and the primary host
    answers. Returns True when the primary was restored."""
    record = getattr(agent, "_transport_fallback", None)
    if not isinstance(record, dict) or getattr(agent, "_fallback_activated", False) is not True:
        return False
    if getattr(agent, "_keep_on_fallback_this_turn", False) is True:
        return False  # the user pinned this route with /keep
    interval = _cfg_number(agent, "primary_restore_interval_s", _DEFAULT_RESTORE_INTERVAL_S)
    current = time.monotonic() if now is None else now
    if interval <= 0 or current - float(record.get("last_probe", 0.0)) < interval:
        return False
    record["last_probe"] = current
    host = str(record.get("primary_host") or "")
    if not host or not _primary_reachable(agent, host):
        logger.debug("Mid-turn primary restore: %s still unreachable", host)
        return False
    from agent.agent_runtime_helpers import restore_primary_runtime

    if not restore_primary_runtime(agent):
        return False
    agent._transport_fallback = None
    logger.warning("%sPrimary %s reachable again after %.0fs; restored mid-turn (%s via %s).",
                   getattr(agent, "log_prefix", ""), host, current - float(record.get("since", current)),
                   getattr(agent, "model", "?"), getattr(agent, "provider", "?"))
    return True
