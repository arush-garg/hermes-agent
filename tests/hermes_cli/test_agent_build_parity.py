"""Classic CLI vs TUI/Desktop gateway: the AIAgent they build from one config must be functionally identical.

Both surfaces are driven end to end (``HermesCLI.__init__`` -> ``_init_agent`` and ``tui_gateway.server._make_agent``)
with ``AIAgent`` and provider resolution stubbed; only presentation kwargs (callbacks, platform, session store
handle, client-surface toolsets) may differ.
"""

from __future__ import annotations

import copy
import types

import pytest

from hermes_cli.auth import AuthError
from tests.hermes_cli.test_cli_init import _make_cli

# Functional AIAgent kwargs both surfaces must agree on.
FUNCTIONAL_KEYS = (
    "model", "provider", "base_url", "api_key", "api_mode", "max_iterations", "reasoning_config",
    "service_tier", "request_overrides", "disabled_toolsets", "providers_allowed", "providers_ignored",
    "providers_order", "provider_sort", "provider_require_parameters", "provider_data_collection",
    "openrouter_min_coding_score", "ephemeral_system_prompt", "prefill_messages", "fallback_model",
    "checkpoints_enabled", "checkpoint_interval", "checkpoint_max_snapshots", "checkpoint_max_total_size_mb",
    "checkpoint_max_file_size_mb", "pass_session_id", "skip_context_files", "skip_memory",
)

BASE_CFG = {
    "model": {"default": "anthropic/claude-opus-4.6", "provider": "openrouter"},
    "display": {"compact": False, "tool_progress": "all"},
    "terminal": {"env_type": "local"},
    "agent": {"max_turns": 90, "service_tier": "fast", "system_prompt": "Be terse.",
              "disabled_toolsets": ["browser"]},
    "checkpoints": {"enabled": True, "max_snapshots": 7, "checkpoint_interval": 3},
    "openrouter": {"min_coding_score": 0.4},
    "provider_routing": {"sort": "price", "only": ["anthropic"]},
    "fallback_providers": [{"provider": "anthropic", "model": "claude-sonnet-4-6"}],
}


def _fake_runtime(fail_provider=None):
    def resolve(requested=None, explicit_api_key=None, explicit_base_url=None, target_model=None, **_kw):
        provider = (requested or "openrouter").lower()
        if provider == fail_provider:
            raise AuthError(f"{provider} credentials expired")
        return {"provider": provider, "requested_provider": provider, "api_mode": "chat_completions",
                "base_url": explicit_base_url or f"https://{provider}.example/v1",
                "api_key": explicit_api_key or f"key-{provider}", "command": None, "args": [],
                "credential_pool": None, "source": "test"}
    return resolve


@pytest.fixture
def stubbed(monkeypatch):
    for var in ("HERMES_EPHEMERAL_SYSTEM_PROMPT", "HERMES_INFERENCE_PROVIDER", "HERMES_MODEL",
                "HERMES_INFERENCE_MODEL", "HERMES_TUI_PROVIDER", "HERMES_TUI_CHECKPOINTS", "HERMES_TUI_MAX_TURNS",
                "HERMES_TUI_PASS_SESSION_ID", "HERMES_TUI_SKILLS", "HERMES_IGNORE_RULES", "HERMES_PREFILL_MESSAGES_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("hermes_cli.models.resolve_fast_mode_overrides",
                        lambda model, provider=None, base_url=None: {"service_tier": "priority"})
    monkeypatch.setattr("hermes_cli.agent_build_options.register_config_hooks", lambda *_a, **_kw: None)
    monkeypatch.setattr("hermes_cli.mcp_startup.ensure_mcp_discovery_before_agent_build", lambda **_kw: None)
    return monkeypatch


def _capture(monkeypatch):
    captured = {}

    def fake_agent(**kwargs):
        captured.clear()
        captured.update(kwargs)
        return types.SimpleNamespace(model=kwargs.get("model"), session_id=kwargs.get("session_id"))

    monkeypatch.setattr("run_agent.AIAgent", fake_agent)
    return captured


def _build_cli(monkeypatch, cfg, fail_provider=None):
    import sys
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", _fake_runtime(fail_provider))
    cli_obj = _make_cli(config_overrides=copy.deepcopy(cfg))
    monkeypatch.setattr(sys.modules["cli"], "_prepare_deferred_agent_startup", lambda: None)
    monkeypatch.setattr(cli_obj, "_install_tool_callbacks", lambda: None)
    cli_obj._session_db = object()
    captured = _capture(monkeypatch)
    # Same order as the chat turn: route (fast overrides) first, then the first-use build.
    route = cli_obj._resolve_turn_agent_config("hi")
    cli_obj._init_agent(request_overrides=route.get("request_overrides"))
    assert captured, "CLI never constructed AIAgent"
    return dict(captured)


def _build_tui(monkeypatch, cfg, fail_provider=None):
    from tui_gateway import server
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", _fake_runtime(fail_provider))
    monkeypatch.setattr(server, "_load_cfg", lambda: copy.deepcopy(cfg))
    monkeypatch.setattr("hermes_cli.config.load_config", lambda *_a, **_kw: copy.deepcopy(cfg))
    monkeypatch.setattr(server, "_load_enabled_toolsets", lambda *_a, **_kw: None)
    monkeypatch.setattr(server, "_get_db", lambda: None)
    monkeypatch.setattr(server, "_agent_cbs", lambda sid: {})
    captured = _capture(monkeypatch)
    server._make_agent("sid", "session-key")
    return dict(captured)


def _functional(kwargs):
    out = {k: kwargs.get(k) for k in FUNCTIONAL_KEYS}
    for k in ("request_overrides", "prefill_messages", "disabled_toolsets"):
        out[k] = out[k] or None  # {} / [] / None are the same "unset"
    return out


def test_cli_and_tui_build_identical_agent_options(stubbed):
    cli_kwargs = _functional(_build_cli(stubbed, BASE_CFG))
    tui_kwargs = _functional(_build_tui(stubbed, BASE_CFG))
    assert cli_kwargs == tui_kwargs
    # Guard against a vacuous pass: the config knobs really reached the agent.
    assert tui_kwargs["checkpoints_enabled"] is True and tui_kwargs["checkpoint_interval"] == 3
    assert tui_kwargs["checkpoint_max_snapshots"] == 7
    assert tui_kwargs["openrouter_min_coding_score"] == 0.4
    assert tui_kwargs["service_tier"] == "priority"
    assert tui_kwargs["request_overrides"] == {"service_tier": "priority"}
    assert tui_kwargs["ephemeral_system_prompt"] == "Be terse."
    assert tui_kwargs["max_iterations"] == 90


def test_cli_and_tui_take_the_same_auth_fallback(stubbed):
    cli_kwargs = _functional(_build_cli(stubbed, BASE_CFG, fail_provider="openrouter"))
    tui_kwargs = _functional(_build_tui(stubbed, BASE_CFG, fail_provider="openrouter"))
    assert (cli_kwargs["model"], cli_kwargs["provider"]) == ("claude-sonnet-4-6", "anthropic")
    assert (tui_kwargs["model"], tui_kwargs["provider"]) == ("claude-sonnet-4-6", "anthropic")
    assert cli_kwargs["base_url"] == tui_kwargs["base_url"] and cli_kwargs["api_key"] == tui_kwargs["api_key"]


@pytest.mark.parametrize("raw, expected", [("fast", "priority"), ("normal", None), ("off", None),
                                           ("auto", "auto"), ("cold", "cold"), ("bogus", None)])
def test_service_tier_words_parse_identically(raw, expected):
    from hermes_cli.agent_build_options import service_tier
    from hermes_cli.cli_config_load import _parse_service_tier_config
    from tui_gateway import server
    assert service_tier({"agent": {"service_tier": raw}}) == _parse_service_tier_config(raw) == expected
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(server, "_load_cfg", lambda: {"agent": {"service_tier": raw}})
        assert server._load_service_tier() == expected


def test_resume_restores_persisted_yolo_on_both_surfaces(monkeypatch):
    import json
    from hermes_cli.agent_build_options import restore_persisted_session_yolo
    from tools import approval
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    row = {"model_config": json.dumps({"yolo_mode": True})}
    approval.disable_session_yolo("parity-yolo")
    try:
        assert restore_persisted_session_yolo(row, "parity-yolo") is True
        assert approval.is_session_yolo_enabled("parity-yolo")
        assert restore_persisted_session_yolo(row, "parity-yolo") is False  # already on: idempotent
        assert restore_persisted_session_yolo({"model_config": "{bad"}, "other") is False
    finally:
        approval.disable_session_yolo("parity-yolo")
