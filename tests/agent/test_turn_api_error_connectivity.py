"""Wiring tests for the connectivity-pause feature (Wi-Fi outage pause/resume).

Covers the ``handle_api_error`` detour placement + ``PauseVerdict`` mapping, the
resume keep-guard in ``settle_unrecovered_error`` / ``nous_rate_limit_guard``,
guard clearing when the resumed call is issued, and the
``agent.connectivity_pause`` config defaults + validation.

The real ``agent/connectivity_probe.py`` and ``agent/turn_connectivity_pause.py``
are exercised; only the two network/blocking seams inside the pause module are
stubbed (``_probe`` — no sockets; ``_wait_for_connectivity`` — no blocking
wait). The stubbed wait mimics the real engagement signal
(``retry_state.connectivity_pauses`` bump + the ``_connectivity_resume_keep``
pin) so the detour's decline-vs-resume disambiguation is tested for real.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.connectivity_probe import ProbeOutcome
from agent.error_classifier import FailoverReason
from agent.turn_api_error import handle_api_error, settle_unrecovered_error
from agent.turn_api_call import nous_rate_limit_guard, perform_api_call
from agent.turn_connectivity_pause import PauseVerdict
from agent.turn_retry_state import TurnRetryState


# ---------------------------------------------------------------------------
# Pause-module seams: stub the network probe and the blocking wait
# ---------------------------------------------------------------------------

@pytest.fixture
def pause_script(monkeypatch):
    """Script the pause module's blocking/network seams.

    ``pause_script.probe``: ProbeOutcome returned by the confirmation probe.
    ``pause_script.wait_verdict``: PauseVerdict returned by the (stubbed) wait;
    the stub bumps ``connectivity_pauses`` and sets the resume keep-pin exactly
    like the real ``_wait_for_connectivity`` does on engagement.
    """
    import agent.turn_connectivity_pause as tcp

    script = SimpleNamespace(
        probe=ProbeOutcome(provider_reachable=False, network_reachable=False),
        wait_verdict=PauseVerdict("continue"),
        wait_calls=[],
        probe_calls=[],
    )

    def fake_probe(agent, provider_host, cfg):
        script.probe_calls.append((provider_host, cfg))
        return script.probe

    def fake_wait(agent, retry_state, cfg, provider_host, *,
                  messages, conversation_history, api_call_count):
        script.wait_calls.append((provider_host, messages, api_call_count))
        # Engagement signal the detour keys on (mirrors the real function).
        retry_state.connectivity_pauses = (
            int(getattr(retry_state, "connectivity_pauses", 0) or 0) + 1)
        if script.wait_verdict.action == "continue" or \
                script.wait_verdict.restart_with_rebuilt_messages:
            agent._connectivity_resume_keep = True
        return script.wait_verdict

    monkeypatch.setattr(tcp, "_probe", fake_probe)
    monkeypatch.setattr(tcp, "_wait_for_connectivity", fake_wait)
    return script


# ---------------------------------------------------------------------------
# Fake agent + handle_api_error scaffolding
# ---------------------------------------------------------------------------

def _dns_error():
    import socket
    return socket.gaierror("Name or service not known")


def _tcp_error():
    return ConnectionRefusedError("connection refused")


def _http_error():
    err = RuntimeError("401 Unauthorized")
    err.status_code = 401
    return err


def make_agent(**over):
    agent = SimpleNamespace(
        thinking_callback=None,
        _interrupt_requested=False,
        log_prefix="",
        provider="openrouter",
        model="m",
        base_url="https://openrouter.ai/api/v1",
        api_key=None,
        context_compressor=None,
        _connectivity_pause={
            "enabled": True, "consecutive_failures": 2, "probe_interval_s": 10.0,
            "probe_timeout_s": 3.0, "probe_hosts": ["1.1.1.1:443"],
            "max_pause_s": 1800.0, "cache_warm_window_s": 1800.0,
            "resume_keep_provider": True,
        },
        _extract_api_error_context=lambda exc: {},
        _invoke_api_request_error_hook=lambda **kw: None,
        _touch_activity=lambda label: None,
    )
    for key, value in over.items():
        setattr(agent, key, value)
    return agent


def _fallthrough_route(**over):
    base = dict(
        status_code=None, messages=[], active_system_prompt=None,
        conversation_history=[], retry_count=0, max_retries=3,
        compression_attempts=0, is_rate_limited=False,
        wrapped_output_cap_budget=None, is_zai_coding_overload=False,
        provider_overflow_recovery_pending=False, action="fallthrough", result=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _fallthrough_overflow(**over):
    base = dict(
        messages=[], active_system_prompt=None, conversation_history=[],
        approx_tokens=0, compression_attempts=0, is_context_length_error=False,
        provider_overflow_recovery_pending=False, action="fallthrough", result=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def error_pipeline(monkeypatch):
    """Patch the phases around the connectivity detour; record call order and
    the retry_count/compression_attempts each downstream phase observed."""
    import agent.turn_api_error as tae

    order = []
    seen = {}

    def fake_before(agent, api_error, **kw):
        order.append("recover_before")
        return False, kw.get("active_system_prompt")

    def fake_after(agent, api_error, classified, _retry, **kw):
        order.append("recover_after")
        seen["after_retry_count"] = kw.get("retry_count")
        return False, False

    def fake_log(agent, api_error, **kw):
        return ("ConnectionError", "dns", "p", "b", "m")

    def fake_route(agent, api_error, classified, _retry, **kw):
        order.append("route")
        return _fallthrough_route(retry_count=kw.get("retry_count", 0),
                                 compression_attempts=kw.get("compression_attempts", 0))

    def fake_overflow(agent, api_error, classified, _retry, **kw):
        order.append("overflow")
        return _fallthrough_overflow()

    def fake_settle(agent, **kw):
        order.append("settle")
        seen["settle_retry_count"] = kw.get("retry_count")
        return SimpleNamespace(action="continue", active_system_prompt=None,
                               retry_count=0, compression_attempts=0, result=None)

    monkeypatch.setattr(tae, "recover_before_classification", fake_before)
    monkeypatch.setattr(tae, "recover_after_classification", fake_after)
    monkeypatch.setattr(tae, "log_api_error_attempt", fake_log)
    monkeypatch.setattr(tae, "route_classified_error", fake_route)
    monkeypatch.setattr(tae, "recover_from_overflow", fake_overflow)
    monkeypatch.setattr(tae, "settle_unrecovered_error", fake_settle)
    return SimpleNamespace(order=order, seen=seen)


def call_handle_api_error(api_error, agent=None, retry=None, **over):
    agent = agent if agent is not None else make_agent()
    retry = retry if retry is not None else TurnRetryState()
    kw = dict(
        api_error=api_error, _retry=retry, thinking_spinner=None, messages=[],
        api_messages=[], api_kwargs={}, system_message=None,
        active_system_prompt=None, conversation_history=[], approx_tokens=100,
        retry_count=2, max_retries=3, compression_attempts=0,
        max_compression_attempts=3, api_call_count=1, api_request_id="r1",
        api_start_time=time.time(), effective_task_id="t", turn_id="turn1",
    )
    kw.update(over)
    return handle_api_error(agent, **kw), retry


# ---------------------------------------------------------------------------
# Detour placement + decline paths (no pause engaged)
# ---------------------------------------------------------------------------

def test_debounced_tcp_failure_declines_to_normal_path(
        pause_script, error_pipeline):
    """First TCP failure is below consecutive_failures=2: the real pause module
    declines without probing, and the normal path runs with counters untouched."""
    verdict, retry = call_handle_api_error(_tcp_error())

    assert not pause_script.probe_calls, "debounced failures must not probe"
    assert not pause_script.wait_calls
    assert retry.consecutive_transport_failures == 1
    assert error_pipeline.order[0] == "recover_before"
    assert "recover_after" in error_pipeline.order, \
        "detour must run BEFORE recover_after_classification and decline to it"
    assert error_pipeline.order.index("recover_after") < \
        error_pipeline.order.index("settle")
    assert verdict.action == "continue"  # canned settle_unrecovered_error
    assert error_pipeline.seen["settle_retry_count"] == 3, \
        "declined detour leaves retry_count alone (2 + 1 normal increment)"


def test_transient_blip_declines_to_normal_path(pause_script, error_pipeline):
    """Provider reachable on the confirmation probe: transient blip, normal retry.
    The pause module answers 'continue' here — the detour must NOT mistake it
    for a resume."""
    pause_script.probe = ProbeOutcome(
        provider_reachable=True, network_reachable=True)
    verdict, retry = call_handle_api_error(_dns_error())  # DNS fast-paths debounce

    assert pause_script.probe_calls and not pause_script.wait_calls
    assert "recover_after" in error_pipeline.order
    assert error_pipeline.seen["settle_retry_count"] == 3, \
        "a declined 'continue' must not zero the retry counter"
    assert verdict.action == "continue"


def test_provider_outage_declines_to_fallback_ladder(pause_script, error_pipeline):
    """Provider down but the local network up: NOT a local outage — the normal
    recovery/fallback path must handle it, never the pause."""
    pause_script.probe = ProbeOutcome(
        provider_reachable=False, network_reachable=True)
    verdict, retry = call_handle_api_error(_dns_error())

    assert pause_script.probe_calls and not pause_script.wait_calls
    assert "recover_after" in error_pipeline.order, \
        "provider outages retain the normal recovery/fallback path"
    assert error_pipeline.seen["settle_retry_count"] == 3
    assert verdict.action == "continue"


def test_non_transport_failure_resets_streak_and_declines(
        pause_script, error_pipeline):
    retry = TurnRetryState()
    retry.consecutive_transport_failures = 2
    verdict, retry = call_handle_api_error(_http_error(), retry=retry)

    assert retry.consecutive_transport_failures == 0, \
        "non-transport failures reset the debounce streak"
    assert not pause_script.probe_calls and not pause_script.wait_calls
    assert "recover_after" in error_pipeline.order
    assert verdict.action == "continue"


def test_disabled_feature_skips_detour_entirely(pause_script, error_pipeline):
    agent = make_agent(_connectivity_pause={"enabled": False})
    retry = TurnRetryState()
    verdict, retry = call_handle_api_error(_dns_error(), agent=agent, retry=retry)

    assert retry.consecutive_transport_failures == 0, "counter untouched"
    assert not pause_script.probe_calls and not pause_script.wait_calls
    assert "recover_after" in error_pipeline.order
    assert verdict.action == "continue"


# ---------------------------------------------------------------------------
# Pause engaged: verdict mapping
# ---------------------------------------------------------------------------

def _both_unreachable(pause_script):
    pause_script.probe = ProbeOutcome(
        provider_reachable=False, network_reachable=False)


def test_warm_resume_zeroes_retry_and_continues_normal_path(
        pause_script, error_pipeline):
    """Pause engaged, outage within the cache-warm window: fresh retry cycle,
    then the normal path (settle sees the keep pin and suppresses fallback)."""
    _both_unreachable(pause_script)
    pause_script.wait_verdict = PauseVerdict("continue")
    agent = make_agent()
    verdict, retry = call_handle_api_error(_dns_error(), agent=agent)

    assert pause_script.wait_calls, "the blocking wait must have engaged"
    assert agent._connectivity_resume_keep is True, "resume pins the provider"
    assert "recover_after" in error_pipeline.order, \
        "warm resume flows into the normal path, not a short-circuit"
    assert "settle" in error_pipeline.order
    assert error_pipeline.seen["settle_retry_count"] == 1, \
        "resume consumes wall-clock, not attempts: counter zeroed, then +1"
    assert verdict.action == "continue"


def test_cold_resume_arms_rebuilt_messages_break(pause_script, error_pipeline):
    """Outage past cache_warm_window_s: break + restart_with_rebuilt_messages so
    the pre-API preflight re-runs (compaction check), mirroring _arm_fallback_restart
    minus the provider switch."""
    _both_unreachable(pause_script)
    pause_script.wait_verdict = PauseVerdict(
        "break", restart_with_rebuilt_messages=True)
    verdict, retry = call_handle_api_error(
        _dns_error(), retry_count=2, compression_attempts=1)

    assert verdict.action == "break"
    assert retry.restart_with_rebuilt_messages is True
    assert retry.primary_recovery_attempted is False
    assert verdict.retry_count == 0
    assert verdict.compression_attempts == 0
    assert "recover_after" not in error_pipeline.order, "break leaves the loop"


def test_redirect_during_pause_preserved(pause_script, error_pipeline):
    _both_unreachable(pause_script)
    pause_script.wait_verdict = PauseVerdict(
        "break", restart_with_redirected_messages=True)
    verdict, retry = call_handle_api_error(_dns_error())

    assert verdict.action == "break"
    assert retry.restart_with_redirected_messages is True


def test_interrupt_during_pause_returns_abort_result(
        pause_script, error_pipeline):
    result = {"final_response": "Interrupted while waiting for network connectivity.",
              "completed": False, "interrupted": True}
    _both_unreachable(pause_script)
    pause_script.wait_verdict = PauseVerdict("return", result=result)
    verdict, _ = call_handle_api_error(_dns_error())

    assert verdict.action == "return"
    assert verdict.result == result


def test_detour_passes_conversation_state_for_abort_result(
        pause_script, error_pipeline):
    """messages/conversation_history/api_call_count reach enter_connectivity_pause
    so a stop-interrupt during the wait can build a proper abort result."""
    _both_unreachable(pause_script)
    messages = [{"role": "user", "content": "hi"}]
    call_handle_api_error(_dns_error(), messages=messages, api_call_count=7)

    assert pause_script.wait_calls
    _host, got_messages, got_api_calls = pause_script.wait_calls[0]
    assert got_messages == messages
    assert got_api_calls == 7


# ---------------------------------------------------------------------------
# settle_unrecovered_error: resume keep-pin behaves like /keep
# ---------------------------------------------------------------------------

def _settle_kwargs(classified, agent=None, retry=None, **over):
    agent = agent if agent is not None else make_agent()
    retry = retry if retry is not None else TurnRetryState()
    kw = dict(
        api_error=_http_error(), classified=classified, _retry=retry,
        status_code=401, error_msg="401 Unauthorized", is_context_length_error=False,
        is_rate_limited=False, _is_zai_coding_overload=False, _provider="openrouter",
        _base="https://openrouter.ai/api/v1", _model="m", messages=[],
        api_messages=[], api_kwargs={}, active_system_prompt=None,
        conversation_history=[], approx_tokens=10, retry_count=0, max_retries=3,
        compression_attempts=0, api_call_count=1, error_context=None,
        current_turn_user_idx=None,
    )
    kw.update(over)
    return agent, kw, retry


def _client_error_classified():
    from agent.error_classifier import classify_api_error
    return classify_api_error(
        _http_error(), provider="openrouter", model="m", approx_tokens=10,
        context_length=200000, num_messages=2,
        base_url="https://openrouter.ai/api/v1", api_key=None)


@pytest.fixture
def settle_agent():
    return make_agent(
        _connectivity_resume_keep=False,
        _try_activate_fallback=Mock(return_value=True),
        _has_pending_fallback=Mock(return_value=True),
        _try_recover_primary_transport=Mock(return_value=False),
        _buffer_diagnostic_status=Mock(),
        _buffer_vprint=Mock(),
        _persist_session=Mock(),
    )


@pytest.fixture
def settle_terminal(monkeypatch):
    import agent.turn_api_error as tae
    monkeypatch.setattr(tae, "settle_delivered_partial",
                        lambda agent, messages, idx: None)
    monkeypatch.setattr(
        tae, "nonretryable_client_error_result",
        lambda agent, api_error, classified, **kw: {
            "final_response": "terminal", "completed": False})


@pytest.fixture
def no_rejected_model_mark(monkeypatch):
    monkeypatch.setattr(
        "agent.fallback_cooldown._mark_entitlement_rejected_model",
        lambda agent, exc: None)


@pytest.fixture
def fake_arm_restart(monkeypatch):
    import agent.conversation_loop as cl

    def _fake(agent, api_messages, active_system_prompt, _retry):
        _retry.restart_with_rebuilt_messages = True
        return active_system_prompt

    monkeypatch.setattr(cl, "_arm_fallback_restart", _fake)


def test_keep_pin_skips_fallback_activation_like_keep(
        settle_agent, settle_terminal, no_rejected_model_mark):
    """A pinned resume cycle must not bounce the turn to another provider —
    same semantics as /keep_on_fallback_this_turn."""
    settle_agent._connectivity_resume_keep = True
    agent, kw, _ = _settle_kwargs(_client_error_classified(), agent=settle_agent)
    verdict = settle_unrecovered_error(agent, **kw)

    assert verdict.action == "return"
    assert settle_agent._try_activate_fallback.call_count == 0
    assert settle_agent._keep_on_fallback_this_turn is True, \
        "resume pin flows through the same /keep flag for the resume cycle"


def test_no_pin_allows_fallback(settle_agent, settle_terminal,
                                no_rejected_model_mark, fake_arm_restart):
    settle_agent._connectivity_resume_keep = False
    agent, kw, retry = _settle_kwargs(_client_error_classified(), agent=settle_agent)
    verdict = settle_unrecovered_error(agent, **kw)

    assert verdict.action == "break"
    assert settle_agent._try_activate_fallback.call_count == 1
    assert retry.restart_with_rebuilt_messages is True


def test_max_retries_pin_skips_fallback_goes_to_autorecover(
        settle_agent, monkeypatch):
    """After max retries, a pinned resume cycle must not activate fallback even
    when one is pending — it goes straight to the exhausted-recovery ladder."""
    import agent.turn_recovery_autorecover as tara
    monkeypatch.setattr(
        tara, "auto_recover_after_exhaustion",
        lambda agent, *a, **kw: {"action": "continue"})
    settle_agent._connectivity_resume_keep = True
    classified = SimpleNamespace(reason=FailoverReason.server_error, retryable=True,
                                 should_compress=False, should_fallback=True)
    agent, kw, retry = _settle_kwargs(classified, agent=settle_agent,
                               retry_count=3, max_retries=3)
    retry.primary_recovery_attempted = True  # skip the primary-recovery branch
    verdict = settle_unrecovered_error(agent, **kw)

    assert verdict.action == "continue"
    assert settle_agent._try_activate_fallback.call_count == 0
    assert settle_agent._keep_on_fallback_this_turn is True


# ---------------------------------------------------------------------------
# perform_api_call: pin consumed on call issue; nous pre-call guard honors pin
# ---------------------------------------------------------------------------

def _perform_kwargs(agent=None, retry=None):
    agent = agent if agent is not None else make_agent(
        api_mode="chat_completions",
        client=Mock(),
        session_id="s",
        platform="cli",
        _pending_redirect_lock=None,
        _model_request_active=None,
        _has_pending_redirect=lambda: False,
        _has_stream_consumers=lambda: False,
        _interruptible_api_call=lambda kw: "x",
    )
    retry = retry if retry is not None else TurnRetryState()
    kw = dict(
        agent=agent, api_kwargs={}, _original_api_kwargs={}, _llm_middleware_trace=[],
        _moa_prepared_request=None, _retry=retry, thinking_spinner=None,
        retry_count=0, api_call_count=0, api_request_id="r1",
        effective_task_id="t", turn_id="turn1", interrupted=False,
    )
    return kw, agent


def test_pin_cleared_when_resumed_call_issued(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.middleware.run_llm_execution_middleware",
        lambda *a, **k: "RESP")
    kw, agent = _perform_kwargs()
    agent._connectivity_resume_keep = True

    verdict = perform_api_call(**kw)

    assert agent._connectivity_resume_keep is False, \
        "pin is one-shot: consumed the moment the resumed call is issued"
    assert getattr(agent, "_keep_on_fallback_this_turn", False) is False, \
        "a clean resume records no /keep flag — the next turn restores normally"
    assert verdict.action == "fallthrough"
    assert verdict.response == "RESP"


def test_provider_side_failure_after_resume_reenables_fallback(
        settle_agent, settle_terminal, no_rejected_model_mark, fake_arm_restart,
        monkeypatch):
    """After the resumed call is issued the pin is gone, so a subsequent
    provider-side failure (non-transport) may fall back normally."""
    monkeypatch.setattr(
        "hermes_cli.middleware.run_llm_execution_middleware",
        lambda *a, **k: "RESP")
    pkw, pagent = _perform_kwargs()
    pagent._connectivity_resume_keep = True
    perform_api_call(**pkw)
    assert pagent._connectivity_resume_keep is False

    settle_agent._connectivity_resume_keep = False
    agent, kw, retry = _settle_kwargs(_client_error_classified(), agent=settle_agent)
    verdict = settle_unrecovered_error(agent, **kw)

    assert verdict.action == "break"
    assert settle_agent._try_activate_fallback.call_count == 1


def test_nous_pre_call_guard_honors_resume_pin(monkeypatch):
    """The Nous rate-limit pre-call guard must not switch providers on a pinned
    resume cycle."""
    monkeypatch.setattr(
        "hermes_cli.anon_auth.apply_model_switch", lambda agent, model: None)
    monkeypatch.setattr(
        "agent.nous_rate_guard.nous_rate_limit_remaining",
        lambda anonymous=False: 60)
    monkeypatch.setattr(
        "agent.nous_rate_guard.format_remaining", lambda seconds: "60s")
    monkeypatch.setattr(
        "hermes_cli.anon_auth.is_anonymous_agent", lambda agent: False)
    agent = make_agent(
        provider="nous", _model="nous:research",
        _connectivity_resume_keep=True,
        _try_activate_fallback=Mock(return_value=True),
        _has_pending_fallback=Mock(return_value=True),
        _buffer_diagnostic_status=Mock(),
        _buffer_vprint=Mock(),
        _persist_session=Mock(),
        _flush_status_buffer=Mock(),
    )
    verdict = nous_rate_limit_guard(
        agent, _retry=TurnRetryState(), api_messages=[], messages=[],
        conversation_history=[], active_system_prompt=None, retry_count=0,
        compression_attempts=0, api_call_count=1)

    assert verdict.action == "return"
    assert agent._try_activate_fallback.call_count == 0, \
        "pre-call fallback must stay suppressed for the pinned resume cycle"
    assert agent._keep_on_fallback_this_turn is True


# ---------------------------------------------------------------------------
# Config: defaults registered, loader round-trip, validation
# ---------------------------------------------------------------------------

_CONNECTIVITY_PAUSE_KEYS = {
    "enabled", "consecutive_failures", "probe_interval_s", "probe_timeout_s",
    "probe_hosts", "max_pause_s", "cache_warm_window_s", "resume_keep_provider",
}


def test_config_defaults_registered():
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    section = DEFAULT_CONFIG["agent"]["connectivity_pause"]
    assert set(section) == _CONNECTIVITY_PAUSE_KEYS, \
        "defaults and the agent_init reader must agree on the key contract"
    assert isinstance(section["enabled"], bool)
    assert isinstance(section["resume_keep_provider"], bool)
    assert isinstance(section["probe_hosts"], list) and section["probe_hosts"]
    assert isinstance(section["consecutive_failures"], int)


def test_loader_round_trip_through_temp_config_yaml(tmp_path, monkeypatch):
    """The temp config.yaml key must flow through the loader into the agent_init
    reader with defaults merged in (the convention in hermes_cli/AGENTS.md)."""
    from hermes_cli.config import load_config
    from agent.agent_init import resolve_connectivity_pause_settings

    (tmp_path / "config.yaml").write_text(
        "agent:\n  connectivity_pause:\n"
        "    enabled: false\n    probe_interval_s: 5\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = load_config()

    settings = resolve_connectivity_pause_settings(
        cfg["agent"]["connectivity_pause"])
    assert settings["enabled"] is False
    assert settings["probe_interval_s"] == 5.0
    assert settings["consecutive_failures"] == \
        resolve_connectivity_pause_settings({})["consecutive_failures"], \
        "unspecified keys keep defaults"


def test_resolve_settings_rejects_garbage():
    from agent.agent_init import resolve_connectivity_pause_settings
    defaults = resolve_connectivity_pause_settings({})
    settings = resolve_connectivity_pause_settings({
        "enabled": "definitely", "probe_interval_s": -3, "probe_hosts": [],
        "max_pause_s": -1})
    for key in ("enabled", "probe_interval_s", "probe_hosts", "max_pause_s"):
        assert settings[key] == defaults[key], \
            f"invalid {key} must fail closed to the default, not the bad value"


def test_validate_connectivity_pause():
    from copy import deepcopy
    from hermes_cli.config import _validate_connectivity_pause
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    good = deepcopy(DEFAULT_CONFIG["agent"]["connectivity_pause"])
    issues = []
    _validate_connectivity_pause(good, issues)
    assert issues == []

    bad = deepcopy(good)
    bad.update({"enabled": "yes", "consecutive_failures": 0, "probe_interval_s": -1,
                "max_pause_s": -1, "probe_hosts": [], "resume_keep_provider": 1})
    issues = []
    _validate_connectivity_pause(bad, issues)
    assert len(issues) >= 5
    assert all(issue.severity == "error" for issue in issues)

    issues = []
    _validate_connectivity_pause("not-a-dict", issues)
    assert issues != []
    issues = []
    _validate_connectivity_pause({"max_pause_s": 0}, issues)
    assert issues == [], "0 = wait indefinitely is explicitly allowed"

def test_resume_pinned_uses_identity_not_truthiness():
    """Regression: Mock agents and ``__getattr__`` fakes answer any attribute with
    a truthy object — that must never read as a set resume pin (it suppressed
    fallback in test_turn_recovery_autorecover.py before the fix)."""
    from types import SimpleNamespace
    from unittest.mock import Mock
    from agent.turn_api_error import _connectivity_resume_pinned

    assert _connectivity_resume_pinned(
        SimpleNamespace(_connectivity_resume_keep=True)) is True
    assert _connectivity_resume_pinned(
        SimpleNamespace(_connectivity_resume_keep=False)) is False
    assert _connectivity_resume_pinned(SimpleNamespace()) is False

    class GetattrFake:
        def __getattr__(self, name):
            return lambda *a, **k: None

    assert _connectivity_resume_pinned(GetattrFake()) is False
    assert _connectivity_resume_pinned(Mock()) is False
