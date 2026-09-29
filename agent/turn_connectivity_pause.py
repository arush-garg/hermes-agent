"""Connectivity-loss pause for the turn's API retry loop.

When a model API call fails with a transport-classified error (DNS / TCP /
connect timeout — see ``agent/connectivity_probe.py``), the failure may be
local Wi-Fi loss rather than a provider problem. Burning retries, credential
rotations and provider fallbacks against a dead local network is pure waste:
this module pauses the turn *in place* — a visible "waiting for connectivity"
status, no message or system-prompt mutation (prompt-caching invariant) — and
resumes on the same provider when connectivity returns.

Wiring contract (the ``turn_api_error`` hook owns this; this module never
touches the loop directly):

- On every transport-classified API failure the hook calls
  ``note_transport_failure(retry_state, kind)``; on API success or a
  non-transport failure it calls ``reset_transport_failures(retry_state)``.
- The hook then calls ``enter_connectivity_pause(agent, exc, retry_state,
  ...)``. The returned ``PauseVerdict`` maps onto the ``handle_api_error``
  protocol: ``"continue"`` → retry the call, ``"break"`` → leave the retry
  loop (``restart_with_*`` flags are already armed on ``retry_state``),
  ``"return"`` → end the turn with ``verdict.result``.
- Resume verdicts consume wall-clock, not attempts: on any resume verdict the
  hook must zero its ``retry_count`` local (and ``compression_attempts`` too
  for the ``"break"`` / cold-cache path) — the same caller contract as
  ``conversation_loop._arm_fallback_restart``.
- On resume this module sets ``agent._connectivity_resume_keep = True``
  (one-shot): the hook's ``settle_unrecovered_error`` path must skip
  ``_try_activate_fallback`` while it is set, then clear it when the resumed
  API call is issued — the same pinning the ``/keep`` command applies.

Config contract (wiring: ``agent.connectivity_pause`` in config.yaml →
``agent._connectivity_pause`` dict; every key optional, defaults below):

- ``enabled`` (True): master switch.
- ``consecutive_failures`` (2): transport failures before a pause is allowed.
- ``probe_interval_s`` (10): re-probe cadence while paused.
- ``probe_timeout_s`` (3): per-probe socket timeout.
- ``probe_hosts`` (["1.1.1.1:443", "8.8.8.8:53"]): neutral endpoints proving
  the local network is up.
- ``max_pause_s`` (1800): pause cap; cron runs are capped at 300 instead.
- ``cache_warm_window_s`` (1800): outages longer than this resume through the
  pre-API preflight (compaction check), mirroring the fallback restart.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger("agent.conversation_loop")

# Transport failure kinds (agent/connectivity_probe.py::classify_transport_failure)
# that can indicate local connectivity loss rather than a provider problem.
_LOCAL_LOSS_KINDS = ("dns", "tcp", "timeout")

# Slice granularity for interrupt checks inside the pause wait, mirroring
# interruptible_backoff_sleep: 200 ms slices, activity touched every 150.
_PAUSE_SLICE_S = 0.2
_PAUSE_TOUCH_EVERY_SLICES = 150  # 150 × 0.2s = 30s

# Cron workers must not hold a scheduler slot for a full pause: the effective
# cap is min(max_pause_s, this).
_CRON_PAUSE_CAP_S = 300.0

_DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "consecutive_failures": 2,
    "probe_interval_s": 10.0,
    "probe_timeout_s": 3.0,
    "probe_hosts": ("1.1.1.1:443", "8.8.8.8:53"),
    "max_pause_s": 1800.0,
    "cache_warm_window_s": 1800.0,
}


@dataclass
class PauseVerdict:
    """Decision from ``enter_connectivity_pause`` for the ``handle_api_error``
    protocol: ``"continue"`` (retry the API call), ``"break"`` (leave the retry
    loop — ``restart_with_*`` flags are armed on the retry state),
    ``"return"`` (end the turn with ``result``)."""

    action: str
    result: Any = None
    restart_with_rebuilt_messages: bool = False
    restart_with_redirected_messages: bool = False


def note_transport_failure(retry_state: Any, kind: str) -> None:
    """Record one more consecutive transport-classified failure on this attempt.

    The wiring hook calls this for every transport failure (including the one
    that triggers a pause) *before* ``enter_connectivity_pause``; it calls
    ``reset_transport_failures`` on API success or a non-transport failure."""
    retry_state.consecutive_transport_failures = (
        int(getattr(retry_state, "consecutive_transport_failures", 0) or 0) + 1
    )


def reset_transport_failures(retry_state: Any) -> None:
    """Clear the consecutive-transport-failure counter (success / non-transport)."""
    retry_state.consecutive_transport_failures = 0


def _pause_config(agent: Any) -> Dict[str, Any]:
    """Effective pause config: ``agent._connectivity_pause`` over the defaults."""
    raw = getattr(agent, "_connectivity_pause", None) or {}
    cfg = dict(_DEFAULTS)
    if isinstance(raw, dict):
        cfg.update(raw)
    return cfg


def _num(cfg: Dict[str, Any], key: str, minimum: Optional[float] = None) -> float:
    """Coerce a numeric config value, falling back to the default on garbage."""
    try:
        value = float(cfg.get(key, _DEFAULTS[key]))
    except (TypeError, ValueError):
        value = float(_DEFAULTS[key])
    if minimum is not None:
        value = max(minimum, value)
    return value


def _provider_host(agent: Any) -> str:
    """Hostname of the provider endpoint, via the probe module's
    ``provider_host_for`` (documented last-resort fallback included). Falls
    back to a local ``base_url`` derivation — and "" when that yields nothing,
    in which case the caller skips pausing — if the probe module is
    unavailable."""
    try:
        from agent.connectivity_probe import provider_host_for

        return provider_host_for(agent)
    except Exception:
        logger.debug("provider_host_for unavailable; deriving from base_url", exc_info=True)
        return (urlparse(str(getattr(agent, "base_url", "") or "")).hostname or "").lower()


def _classify(exc: BaseException) -> str:
    """Late import (patch-where-production-reads): the probe module is owned by
    a sibling builder, so a missing/broken classifier must not take the loop."""
    try:
        from agent.connectivity_probe import classify_transport_failure

        return classify_transport_failure(exc)
    except Exception:
        logger.debug("classify_transport_failure failed; treating as non-transport", exc_info=True)
        return "other"


def _probe(agent: Any, provider_host: str, cfg: Dict[str, Any]) -> Any:
    """Late import; raises only if the probe module itself is broken."""
    from agent.connectivity_probe import probe_connectivity

    return probe_connectivity(
        provider_host,
        probe_hosts=list(cfg.get("probe_hosts") or _DEFAULTS["probe_hosts"]),
        timeout_s=_num(cfg, "probe_timeout_s", minimum=0.1),
    )


def enter_connectivity_pause(
    agent: Any,
    exc: BaseException,
    retry_state: Any,
    *,
    messages: Optional[List[Dict[str, Any]]] = None,
    conversation_history: Any = None,
    api_call_count: int = 0,
) -> PauseVerdict:
    """Decide eligibility, and if eligible run the blocking pause loop until
    connectivity returns, an interrupt arrives, or the cap expires.

    ``messages``/``conversation_history``/``api_call_count`` are optional so the
    stop-interrupt path can build a proper abort result via
    ``abort_turn_on_interrupt`` (persist + close the tool sequence); without
    them a minimal interrupted result is returned instead.
    """
    cfg = _pause_config(agent)
    if not cfg.get("enabled", True):
        return PauseVerdict("continue")

    kind = _classify(exc)
    if kind not in _LOCAL_LOSS_KINDS:
        return PauseVerdict("continue")

    # Debounce: strong DNS failures are unambiguous enough to fast-path after
    # one; other transport kinds need consecutive_failures in a row (the hook
    # counted this failure already via note_transport_failure).
    needed = 1 if kind == "dns" else max(0, int(_num(cfg, "consecutive_failures", minimum=0)))
    seen = int(getattr(retry_state, "consecutive_transport_failures", 0) or 0)
    if seen < needed:
        logger.debug(
            "Connectivity pause debounced: %d/%d consecutive %s failures",
            seen, needed, kind,
        )
        return PauseVerdict("continue")

    provider_host = _provider_host(agent)
    if not provider_host:
        # No provider host to probe against: cannot distinguish local loss from
        # a provider outage, so never pause on an unconfirmed signal.
        logger.debug("Connectivity pause skipped: no provider host derivable from base_url")
        return PauseVerdict("continue")

    try:
        outcome = _probe(agent, provider_host, cfg)
    except Exception:
        logger.debug("Connectivity confirmation probe failed; not pausing", exc_info=True)
        return PauseVerdict("continue")

    if outcome.provider_reachable:
        return PauseVerdict("continue")  # transient blip; normal retry
    if outcome.network_reachable:
        # Provider down but the local network is up: a provider outage, which
        # the existing fallback ladder already handles.
        return PauseVerdict("continue")
    return _wait_for_connectivity(
        agent, retry_state, cfg, provider_host,
        messages=messages, conversation_history=conversation_history,
        api_call_count=api_call_count,
    )


def _wait_for_connectivity(
    agent: Any,
    retry_state: Any,
    cfg: Dict[str, Any],
    provider_host: str,
    *,
    messages: Optional[List[Dict[str, Any]]],
    conversation_history: Any,
    api_call_count: int,
) -> PauseVerdict:
    """Blocking pause loop: re-probe every probe_interval_s until connectivity
    returns, an interrupt arrives, or the cap expires."""
    cap = _num(cfg, "max_pause_s", minimum=0.0)
    if str(getattr(agent, "platform", "") or "") == "cron":
        cap = min(cap, _CRON_PAUSE_CAP_S)
    started = time.monotonic()
    retry_state.pause_started_at = started
    retry_state.connectivity_pauses = int(getattr(retry_state, "connectivity_pauses", 0) or 0) + 1
    deadline = started + cap
    probe_interval = _num(cfg, "probe_interval_s", minimum=_PAUSE_SLICE_S)
    log_prefix = getattr(agent, "log_prefix", "")

    agent._emit_diagnostic_status(
        "⏸️ Local network connectivity lost — pausing retries until connectivity returns."
    )
    logger.warning(
        "%sConnectivity pause: local network loss detected; pausing in place (cap %.0fs).",
        log_prefix, cap,
    )

    touch_counter = 0
    slices = max(1, int(probe_interval / _PAUSE_SLICE_S))
    while True:
        # Interruptible wait: 200 ms slices so stop/steer land promptly.
        for _ in range(slices):
            if getattr(agent, "_interrupt_requested", False):
                if agent.clear_interrupt(preserve_redirect=True):
                    # Steering, not stopping: rebuild the turn from the redirect.
                    retry_state.restart_with_redirected_messages = True
                    agent._emit_diagnostic_status(
                        "↩️ Steering received during connectivity pause — rebuilding the turn."
                    )
                    return PauseVerdict("break", restart_with_redirected_messages=True)
                return PauseVerdict(
                    "return",
                    result=_abort_result(agent, messages, conversation_history, api_call_count),
                )
            time.sleep(_PAUSE_SLICE_S)
            touch_counter += 1
            if touch_counter % _PAUSE_TOUCH_EVERY_SLICES == 0:
                agent._touch_activity(
                    f"Waiting for network connectivity ({time.monotonic() - started:.0f}s elapsed)"
                )
        if time.monotonic() >= deadline:
            # Cap expired: hand back to the normal recovery ladder. The
            # consecutive-failure counter is deliberately preserved so the
            # ladder sees the real failure history.
            agent._emit_diagnostic_status(
                "⏱️ Connectivity pause cap reached — resuming normal error recovery."
            )
            logger.warning(
                "%sConnectivity pause cap (%.0fs) expired; resuming normal recovery.",
                log_prefix, cap,
            )
            return PauseVerdict("continue")
        agent._emit_diagnostic_wait(
            f"📶 Waiting for network connectivity… ({time.monotonic() - started:.0f}s elapsed)"
        )
        try:
            outcome = _probe(agent, provider_host, cfg)
        except Exception:
            logger.debug("Connectivity re-probe failed; still waiting", exc_info=True)
            continue
        if outcome.provider_reachable or outcome.network_reachable:
            return _resume_after_pause(agent, retry_state, cfg, started)


def _resume_after_pause(
    agent: Any, retry_state: Any, cfg: Dict[str, Any], started: float
) -> PauseVerdict:
    """Connectivity is back: pin the provider (/keep) and pick the resume path."""
    outage_s = time.monotonic() - started
    # One-shot keep-guard for the wiring hook: skip _try_activate_fallback for
    # the resume cycle so a massive pre-pause context never lands on a different
    # provider. The hook clears it when the resumed API call is issued.
    agent._connectivity_resume_keep = True
    agent._emit_diagnostic_status(
        f"✅ Network connectivity restored after {outage_s:.0f}s — resuming."
    )
    logger.warning(
        "%sConnectivity pause ended after %.0fs; resuming turn.",
        getattr(agent, "log_prefix", ""), outage_s,
    )
    if outage_s > _num(cfg, "cache_warm_window_s", minimum=0.0):
        # Cache assumed cold: mirror _arm_fallback_restart's arming shape (but
        # NOT the provider switch — the system prompt stays byte-stable) so the
        # pre-API preflight re-runs and compacts if over threshold.
        retry_state.primary_recovery_attempted = False
        retry_state.restart_with_rebuilt_messages = True
        return PauseVerdict("break", restart_with_rebuilt_messages=True)
    return PauseVerdict("continue")


def _abort_result(
    agent: Any,
    messages: Optional[List[Dict[str, Any]]],
    conversation_history: Any,
    api_call_count: int,
) -> Dict[str, Any]:
    """Build the turn-abort result for a stop interrupt during the pause."""
    if messages is not None:
        from agent.turn_recovery import abort_turn_on_interrupt

        return abort_turn_on_interrupt(
            agent, messages, conversation_history, api_call_count,
            abort_message="Connectivity pause interrupted — aborting retries.",
            interrupt_text="Operation interrupted while waiting for network connectivity.",
        )
    # No conversation state was passed: clear the interrupt and return the
    # minimal interrupted result.
    agent.clear_interrupt(hard_cancel=True)
    return {
        "final_response": "Interrupted while waiting for network connectivity.",
        "messages": messages,
        "api_calls": api_call_count,
        "completed": False,
        "interrupted": True,
    }
