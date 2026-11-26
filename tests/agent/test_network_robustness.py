"""Network-failure robustness: DNS-aware probing, definitive-local fast path,
cross-provider circuit breaker, mid-turn primary restore, claude-cli fallback
binding, and an end-to-end replay of the Wi-Fi/DNS outage incident.

Incident shape (2026-11-24): every provider failed with
``APIConnectionError <- ConnectError <- gaierror(EAI_NONAME)`` while IP-literal
neutral probes (1.1.1.1 / 8.8.8.8) still answered, so the pause read the outage
as a provider outage and the fallback ladder walked the whole chain.

Only the socket layer (``socket.create_connection`` / ``socket.getaddrinfo``)
and the pause module's clock are faked; the probe, pause, detour and
``settle_unrecovered_error`` code under test is real.
"""

from __future__ import annotations

import errno
import socket
import time
from types import SimpleNamespace

import pytest

# Collection-time warm-up (before the home-I/O guard activates), same reason as
# tests/agent/test_turn_api_error_connectivity.py.
import agent.agent_init
import agent.agent_runtime_helpers
import agent.conversation_loop
import agent.fallback_cooldown
import agent.provider_route_hooks
import agent.turn_recovery_autorecover
import hermes_cli.config
import hermes_cli.config_defaults

import agent.connectivity_probe as cp
import agent.turn_api_error as tae
import agent.turn_connectivity_pause as tcp
from agent.connectivity_probe import ProbeOutcome, classify_transport_failure, probe_connectivity
from agent.turn_retry_state import TurnRetryState


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeSocket:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _is_ip(host: str) -> bool:
    try:
        socket.inet_aton(host)
        return True
    except OSError:
        return False


class FakeNet:
    """Socket-layer double. ``ip_up``: IP-literal dials answer. ``dns_up``:
    hostnames resolve (and hostname dials connect)."""

    def __init__(self, monkeypatch, *, ip_up=True, dns_up=True):
        self.ip_up, self.dns_up = ip_up, dns_up
        self.dials: list[tuple[str, int]] = []
        self.lookups: list[str] = []
        monkeypatch.setattr(socket, "create_connection", self._connect)
        monkeypatch.setattr(socket, "getaddrinfo", self._resolve)

    def _gaierror(self):
        return socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided, or not known")

    def _connect(self, address, timeout=None, **_kw):
        host, port = address
        self.dials.append((host, port))
        if _is_ip(host):
            if self.ip_up:
                return _FakeSocket()
            raise OSError(errno.ENETUNREACH, "Network is unreachable")
        if not self.dns_up:
            raise self._gaierror()
        return _FakeSocket()

    def _resolve(self, host, *args, **kwargs):
        self.lookups.append(host)
        if not self.dns_up:
            raise self._gaierror()
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 0))]


class FakeTime:
    def __init__(self, now: float = 1000.0):
        self.now = now
        self.on_sleep = None

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.on_sleep:
            self.on_sleep(self)


def _incident_error() -> Exception:
    """``APIConnectionError <- ConnectError <- gaierror`` as httpx/openai chain it."""
    root = socket.gaierror(8, "nodename nor servname provided, or not known")
    mid = ConnectionError("[Errno 8] nodename nor servname provided, or not known")
    mid.__cause__ = root
    top = RuntimeError("Connection error.")
    top.__cause__ = mid
    return top


def _pause_cfg(**over):
    cfg = {
        "enabled": True, "consecutive_failures": 2, "probe_interval_s": 10.0,
        "probe_timeout_s": 1.0, "probe_hosts": ["1.1.1.1:443", "8.8.8.8:53"],
        "dns_probe_hosts": ["dns.google", "one.one.one.one"],
        "max_pause_s": 1800.0, "cache_warm_window_s": 1800.0,
        "resume_keep_provider": True, "breaker_window_s": 60.0,
        "primary_restore_interval_s": 150.0,
    }
    cfg.update(over)
    return cfg


class PauseAgent(SimpleNamespace):
    """Minimal turn-loop double with the status/interrupt surface the pause uses."""

    def __init__(self, **over):
        super().__init__(
            thinking_callback=None, _interrupt_requested=False, log_prefix="",
            provider="anthropic", model="claude-opus", base_url="https://api.anthropic.com",
            api_key=None, context_compressor=None, platform="cli",
            _connectivity_pause=_pause_cfg(), statuses=[], fallback_calls=[],
            _fallback_activated=False,
        )
        for k, v in over.items():
            setattr(self, k, v)

    def _emit_diagnostic_status(self, msg):
        self.statuses.append(msg)

    _buffer_diagnostic_status = _emit_diagnostic_status

    def _emit_diagnostic_wait(self, msg):
        pass

    def _touch_activity(self, *a, **k):
        pass

    def clear_interrupt(self, preserve_redirect=False, hard_cancel=False):
        return False

    def _extract_api_error_context(self, exc):
        return {}

    def _invoke_api_request_error_hook(self, **kw):
        pass

    def _has_pending_fallback(self):
        return True

    def _try_recover_primary_transport(self, *a, **k):
        return False

    def _try_activate_fallback(self, **kw):
        self.fallback_calls.append((self.provider, kw))
        return True


@pytest.fixture
def clock(monkeypatch):
    fake = FakeTime()
    monkeypatch.setattr(tcp, "time", fake)
    return fake


# ---------------------------------------------------------------------------
# 1. DNS-aware probe
# ---------------------------------------------------------------------------

def test_ip_up_dns_down_is_local_loss_not_provider_outage(monkeypatch):
    """IP literals answer but nothing resolves: the network is NOT usable."""
    net = FakeNet(monkeypatch, ip_up=True, dns_up=False)

    outcome = probe_connectivity("api.anthropic.com", timeout_s=1.0)

    assert outcome == ProbeOutcome(provider_reachable=False, network_reachable=False)
    assert net.lookups, "a neutral hostname must be resolved"


def test_provider_outage_with_working_dns_still_reads_as_provider_outage(monkeypatch):
    FakeNet(monkeypatch, ip_up=True, dns_up=True)
    monkeypatch.setattr(cp, "_tcp_reachable", lambda h, p, t: _is_ip(h))  # provider host refuses

    outcome = probe_connectivity("api.anthropic.com", timeout_s=1.0)

    assert outcome == ProbeOutcome(provider_reachable=False, network_reachable=True)


def test_empty_dns_probe_list_disables_the_dns_check(monkeypatch):
    FakeNet(monkeypatch, ip_up=True, dns_up=False)

    outcome = probe_connectivity("api.anthropic.com", timeout_s=1.0, dns_probe_hosts=[])

    assert outcome.network_reachable is True


def test_dns_check_is_bounded_when_the_resolver_hangs(monkeypatch):
    import threading
    gate = threading.Event()
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: gate.wait(30) or [])
    started = time.monotonic()
    try:
        assert cp.dns_resolves(["dns.google"], 0.5) is False
    finally:
        gate.set()
    assert time.monotonic() - started < 5


def test_dns_probe_hosts_config_is_validated_and_resolved():
    from agent.agent_init import resolve_connectivity_pause_settings
    from hermes_cli.config import _validate_connectivity_pause

    settings = resolve_connectivity_pause_settings({"dns_probe_hosts": ["  example.com ", ""]})
    assert settings["dns_probe_hosts"] == ["example.com"]
    assert resolve_connectivity_pause_settings({"dns_probe_hosts": []})["dns_probe_hosts"] == []
    assert resolve_connectivity_pause_settings({"dns_probe_hosts": "nope"})["dns_probe_hosts"]

    issues: list = []
    _validate_connectivity_pause({"dns_probe_hosts": "dns.google", "breaker_window_s": -1,
                                  "primary_restore_interval_s": True}, issues)
    text = " ".join(str(getattr(i, "message", i)) for i in issues)
    for key in ("dns_probe_hosts", "breaker_window_s", "primary_restore_interval_s"):
        assert key in text
    clean: list = []
    _validate_connectivity_pause({"dns_probe_hosts": [], "breaker_window_s": 0,
                                  "primary_restore_interval_s": 150}, clean)
    assert clean == []


# ---------------------------------------------------------------------------
# 2. Definitive local errors skip the debounce
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code", [errno.EADDRNOTAVAIL, errno.ENETDOWN, errno.ENETUNREACH, errno.EHOSTUNREACH])
def test_definitive_local_errno_is_tcp_and_definitive(code):
    exc = RuntimeError("Connection error.")
    exc.__cause__ = OSError(code, "socket failure")
    assert classify_transport_failure(exc) == "tcp"
    assert cp.is_definitive_local_failure(exc) is True


def test_eaddrnotavail_message_alone_classifies_as_tcp():
    assert classify_transport_failure(OSError("[Errno 49] Can't assign requested address")) == "tcp"


def test_connection_refused_is_not_definitive_local():
    assert cp.is_definitive_local_failure(ConnectionRefusedError(errno.ECONNREFUSED, "refused")) is False


def test_eaddrnotavail_pauses_on_the_first_failure(monkeypatch, clock):
    net = FakeNet(monkeypatch, ip_up=False, dns_up=False)
    clock.on_sleep = lambda c: (setattr(net, "ip_up", True), setattr(net, "dns_up", True)) if c.now > 1030 else None
    agent = PauseAgent()
    rs = TurnRetryState()
    tcp.note_transport_failure(rs, "tcp")  # one failure: below consecutive_failures=2

    verdict = tcp.enter_connectivity_pause(agent, OSError(errno.EADDRNOTAVAIL, "Can't assign requested address"), rs)

    assert rs.connectivity_pauses == 1, "a definitive local errno must not be debounced"
    assert verdict.action == "continue"
    assert agent._connectivity_resume_keep is True


# ---------------------------------------------------------------------------
# 3. Cross-provider circuit breaker
# ---------------------------------------------------------------------------

def _detour(agent, err, rs):
    return tae._connectivity_pause_detour(agent, err, rs, messages=[], conversation_history=[], api_call_count=1)


def test_second_host_failing_same_kind_trips_breaker_and_waits(monkeypatch, clock):
    """Neutral probes (IPs + DNS) say the network is fine, yet two different
    provider hosts both fail at DNS: stop the ladder and wait in place."""
    probes = []

    def fake_probe(agent, host, cfg):
        probes.append(host)
        # first call: confirmation probe on anthropic (network up -> provider outage)
        # later: breaker wait re-probes nvidia; reachable after 3 probes.
        return ProbeOutcome(provider_reachable=len(probes) >= 4, network_reachable=True)

    monkeypatch.setattr(tcp, "_probe", fake_probe)
    agent = PauseAgent()
    rs = TurnRetryState()

    assert _detour(agent, _incident_error(), rs) is None  # provider outage -> ladder
    agent.base_url, agent.provider = "https://integrate.api.nvidia.com/v1", "nvidia"  # fallback activated
    verdict = _detour(agent, _incident_error(), TurnRetryState())

    assert verdict is not None and verdict.action == "continue"
    assert probes[1:] == ["integrate.api.nvidia.com"] * 3, "breaker waits for the provider itself"
    assert agent._connectivity_resume_keep is True, "resume pins the provider: no further fallback"


def test_breaker_needs_distinct_hosts_same_kind_within_window():
    from agent.turn_transport_failover import breaker_tripped

    agent = SimpleNamespace(_connectivity_pause=_pause_cfg(breaker_window_s=60))
    prev = ("dns", "api.anthropic.com", 100.0)
    assert breaker_tripped(agent, "dns", "api.openai.com", prev, now=150.0) is True
    assert breaker_tripped(agent, "dns", "api.anthropic.com", prev, now=150.0) is False  # same host
    assert breaker_tripped(agent, "tcp", "api.openai.com", prev, now=150.0) is False     # other kind
    assert breaker_tripped(agent, "dns", "api.openai.com", prev, now=161.0) is False     # too late
    agent._connectivity_pause["breaker_window_s"] = 0
    assert breaker_tripped(agent, "dns", "api.openai.com", prev, now=150.0) is False     # disabled


def test_http_error_between_transport_failures_resets_the_breaker(monkeypatch):
    monkeypatch.setattr(tcp, "_probe", lambda a, h, c: ProbeOutcome(False, True))
    agent = PauseAgent()
    _detour(agent, _incident_error(), TurnRetryState())
    http = RuntimeError("429 Too Many Requests")
    http.status_code = 429
    _detour(agent, http, TurnRetryState())

    assert agent._last_transport_failure is None


# ---------------------------------------------------------------------------
# 4. Mid-turn primary restore after a transport-caused fallback
# ---------------------------------------------------------------------------

@pytest.fixture
def restore_env(monkeypatch):
    import agent.turn_transport_failover as ttf
    state = SimpleNamespace(reachable=False, restores=0, dials=[])

    def fake_restore(agent):
        state.restores += 1
        agent._fallback_activated = False
        agent.provider = "anthropic"
        return True

    monkeypatch.setattr(agent.agent_runtime_helpers, "restore_primary_runtime", fake_restore)
    monkeypatch.setattr(cp, "_tcp_reachable", lambda h, p, t: state.dials.append(h) or state.reachable)
    return state, ttf


def _fallen_back_agent(ttf, err, now=0.0):
    a = SimpleNamespace(
        _connectivity_pause=_pause_cfg(primary_restore_interval_s=150),
        _primary_runtime={"base_url": "https://api.anthropic.com", "provider": "anthropic"},
        _fallback_activated=False, provider="anthropic", log_prefix="",
    )
    ttf.note_fallback_activation(a, err, was_on_primary=True, now=now)
    a._fallback_activated, a.provider = True, "nvidia"
    return a


def test_primary_restored_mid_turn_once_reachable_after_interval(restore_env):
    state, ttf = restore_env
    a = _fallen_back_agent(ttf, _incident_error(), now=0.0)

    assert ttf.maybe_restore_primary_mid_turn(a, now=100.0) is False and not state.dials  # interval not up
    assert ttf.maybe_restore_primary_mid_turn(a, now=151.0) is False                      # still dark
    assert state.dials == ["api.anthropic.com"]
    state.reachable = True
    assert ttf.maybe_restore_primary_mid_turn(a, now=200.0) is False, "probe cadence is per interval"
    assert ttf.maybe_restore_primary_mid_turn(a, now=302.0) is True
    assert state.restores == 1 and a.provider == "anthropic" and a._transport_fallback is None


def test_http_caused_fallback_never_restores_mid_turn(restore_env):
    state, ttf = restore_env
    err = RuntimeError("402 Payment Required: insufficient_quota")
    err.status_code = 402
    a = _fallen_back_agent(ttf, err)
    state.reachable = True

    assert ttf.maybe_restore_primary_mid_turn(a, now=10_000.0) is False
    assert state.restores == 0 and not state.dials


def test_keep_pin_blocks_mid_turn_restore(restore_env):
    state, ttf = restore_env
    a = _fallen_back_agent(ttf, _incident_error())
    a._keep_on_fallback_this_turn = True
    state.reachable = True

    assert ttf.maybe_restore_primary_mid_turn(a, now=10_000.0) is False
    assert state.restores == 0


def test_fallback_hop_keeps_the_original_transport_verdict(restore_env):
    _state, ttf = restore_env
    a = _fallen_back_agent(ttf, _incident_error())
    http = RuntimeError("401 Unauthorized")
    http.status_code = 401
    ttf.note_fallback_activation(a, http, was_on_primary=False)  # nvidia -> codex on a 401
    assert isinstance(a._transport_fallback, dict)


def test_prepare_iteration_restores_and_resyncs_system_prompt(restore_env, monkeypatch):
    """The API-call boundary hook: restore lands before the request is built and
    the in-flight system prompt becomes the primary's byte-identical copy."""
    from agent.turn_iteration_prep import prepare_iteration
    state, ttf = restore_env
    state.reachable = True
    a = _fallen_back_agent(ttf, _incident_error(), now=time.monotonic() - 1000)
    a._transport_fallback["last_probe"] = time.monotonic() - 1000

    def fake_restore(agent):
        agent._fallback_activated = False
        agent._cached_system_prompt = "PRIMARY PROMPT"
        return True

    monkeypatch.setattr(agent.agent_runtime_helpers, "restore_primary_runtime", fake_restore)

    synced = []
    monkeypatch.setattr(agent.conversation_loop, "_sync_failover_system_message",
                        lambda ag, api_messages, prompt: synced.append(prompt) or ag._cached_system_prompt)

    class Stop(AttributeError):
        """Raised by the first unrelated attribute read after the restore hook."""

    class BoundaryAgent(SimpleNamespace):
        def __getattr__(self, name):
            raise Stop(name)

    b = BoundaryAgent(**vars(a), step_callback=None)
    with pytest.raises(Stop):
        prepare_iteration(b, messages=[], api_call_count=2, active_system_prompt="FALLBACK PROMPT")
    assert b._fallback_activated is False and b._transport_fallback is None
    assert synced == ["FALLBACK PROMPT"], "in-flight system prompt must be re-synced after restore"


# ---------------------------------------------------------------------------
# 5. claude-cli fallback binding
# ---------------------------------------------------------------------------

def test_claude_cli_fallback_binds_a_client_with_the_live_agent(monkeypatch):
    from unittest.mock import MagicMock
    from agent.claude_cli_client import ClaudeCLIClient
    from agent.route_binding import bind_route_entry

    agent_obj = MagicMock()
    agent_obj.model, agent_obj.provider, agent_obj.base_url = "claude-opus", "anthropic", "https://api.anthropic.com"
    agent_obj._anthropic_prompt_cache_policy.return_value = (False, False)
    for name in ("_update_fallback_context_compressor", "_reresolve_fallback_reasoning_config",
                 "_rescope_fallback_extra_body", "_rebind_fallback_credential_pool"):
        monkeypatch.setattr(f"agent.chat_completion_helpers.{name}", lambda *a, **k: None)
    monkeypatch.setattr("agent.agent_runtime_helpers.sync_credential_pool_entry_id", lambda *a, **k: None)

    bound = bind_route_entry(agent_obj, {"provider": "claude-cli", "model": "sonnet"}, "claude-cli", "sonnet")

    assert bound == ("claude-opus", "anthropic"), "claude-cli fallback must bind, not report 'not configured'"
    assert isinstance(agent_obj.client, ClaudeCLIClient)
    assert agent_obj.client._agent is agent_obj
    assert agent_obj.provider == "claude-cli" and agent_obj.base_url.startswith("claude-cli://")


# ---------------------------------------------------------------------------
# E2E: the incident sequence through handle_api_error + settle_unrecovered_error
# ---------------------------------------------------------------------------

@pytest.fixture
def pipeline(monkeypatch):
    """Stub only the phases unrelated to transport handling; settle is REAL."""
    monkeypatch.setattr(tae, "recover_before_classification",
                        lambda agent, err, **kw: (False, kw.get("active_system_prompt")))
    monkeypatch.setattr(tae, "recover_after_classification", lambda *a, **kw: (False, False))
    monkeypatch.setattr(tae, "log_api_error_attempt", lambda *a, **kw: ("APIConnectionError", "dns", "p", "b", "m"))
    monkeypatch.setattr(tae, "route_classified_error", lambda *a, **kw: SimpleNamespace(
        status_code=None, messages=[], active_system_prompt=None, conversation_history=[],
        retry_count=kw["retry_count"], max_retries=kw["max_retries"],
        compression_attempts=kw["compression_attempts"], is_rate_limited=False,
        wrapped_output_cap_budget=None, is_zai_coding_overload=False,
        provider_overflow_recovery_pending=False, action="fallthrough", result=None))
    monkeypatch.setattr(tae, "recover_from_overflow", lambda *a, **kw: SimpleNamespace(
        messages=[], active_system_prompt=None, conversation_history=[], approx_tokens=0,
        compression_attempts=0, is_context_length_error=False,
        provider_overflow_recovery_pending=False, action="fallthrough", result=None))
    monkeypatch.setattr(tae, "compute_error_backoff", lambda *a, **kw: 0)
    monkeypatch.setattr(tae, "interruptible_backoff_sleep", lambda *a, **kw: None)


def _handle(agent, err, rs, retry_count):
    return tae.handle_api_error(
        agent, api_error=err, _retry=rs, thinking_spinner=None, messages=[], api_messages=[],
        api_kwargs={}, system_message=None, active_system_prompt=None, conversation_history=[],
        approx_tokens=100, retry_count=retry_count, max_retries=3, compression_attempts=0,
        max_compression_attempts=3, api_call_count=1, api_request_id="r1",
        api_start_time=time.time(), effective_task_id="t", turn_id="turn1",
    )


def test_incident_dns_outage_pauses_without_fallback_and_resumes_on_primary(monkeypatch, clock, pipeline):
    """All hostnames fail with gaierror while IP literals answer. The pause must
    engage on the primary, never activate a fallback, and resume on the primary
    once DNS comes back — even on the call that exhausts max_retries."""
    net = FakeNet(monkeypatch, ip_up=True, dns_up=False)
    clock.on_sleep = lambda c: setattr(net, "dns_up", True) if c.now > 1000 + 120 else None
    agent = PauseAgent()
    rs = TurnRetryState()

    verdict = _handle(agent, _incident_error(), rs, retry_count=2)  # 3rd attempt = exhaustion on base

    assert rs.connectivity_pauses == 1, "IP-up/DNS-down must engage the connectivity pause"
    assert agent.fallback_calls == [], "no fallback may be activated during a local DNS outage"
    assert agent.provider == "anthropic" and agent.base_url == "https://api.anthropic.com"
    assert verdict.action in ("continue", "fallthrough") and verdict.retry_count < 3
    assert any("restored" in s for s in agent.statuses)
    assert "dns.google" in net.lookups
