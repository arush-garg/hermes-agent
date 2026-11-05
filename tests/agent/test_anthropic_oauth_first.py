"""OAuth-first auth policy uses one credential ladder for every native Claude model."""
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.credential_pool import load_pool
from hermes_cli.config import atomic_config_write
from hermes_cli.runtime_provider import resolve_runtime_provider

MODELS = ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-4-5", "claude-future"]


@pytest.fixture
def auth_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    atomic_config_write(tmp_path / "config.yaml", {
        "model": {"provider": "anthropic", "default": MODELS[0]},
        "auth": {"anthropic_oauth_first": True, "adopt_external_logins": False},
    })
    # Deliberately put the paid key first: source priority must not override this policy.
    rows = [
        {"id": "paid", "source": "manual", "auth_type": "api_key", "priority": 0,
         "access_token": "test-paid-key", "label": "paid"},
        {"id": "subscription", "source": "manual:hermes_pkce", "auth_type": "oauth", "priority": 1,
         "access_token": "cc-test-subscription", "label": "subscription"},
    ]
    (tmp_path / "auth.json").write_text(json.dumps({"version": 1, "credential_pool": {"anthropic": rows}}))
    return tmp_path


@pytest.mark.parametrize("model", MODELS)
def test_resolution_rotation_and_recovery(auth_home, model):
    runtime = resolve_runtime_provider(requested="anthropic", target_model=model)
    assert runtime["api_key"] == "cc-test-subscription"
    pool = runtime["credential_pool"]
    fallback = pool.mark_exhausted_and_rotate(
        status_code=429, credential_id="subscription", model=model,
        error_context={"reset_at": time.time() + 120}, failure_reason="rate_limit",
    )
    assert fallback.id == "paid"
    assert resolve_runtime_provider(requested="anthropic", target_model=model)["api_key"] == "test-paid-key"
    assert resolve_runtime_provider(requested="anthropic", target_model="claude-sibling")["api_key"] == "cc-test-subscription"
    pool.reset_statuses()
    assert resolve_runtime_provider(requested="anthropic", target_model=model)["api_key"] == "cc-test-subscription"


@pytest.mark.parametrize("strategy", ["fill_first", "round_robin", "random", "least_used"])
def test_pool_strategy_never_spends_while_oauth_available(auth_home, strategy):
    from hermes_cli.config import load_config
    cfg = load_config()
    cfg["credential_pool_strategies"] = {"anthropic": strategy}
    atomic_config_write(auth_home / "config.yaml", cfg)
    pool = load_pool("anthropic")
    for _ in range(8):
        assert pool.select(model=MODELS[0]).id == "subscription"


def test_direct_token_resolver_uses_same_cooldown_ladder(auth_home, monkeypatch):
    from agent.anthropic_credentials import resolve_anthropic_token
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-env-paid-key")
    assert resolve_anthropic_token(model=MODELS[0]) == "cc-test-subscription"
    pool = load_pool("anthropic")
    pool.mark_exhausted_and_rotate(status_code=429, credential_id="subscription", model=MODELS[0],
                                   error_context={"reset_at": time.time() + 120})
    assert resolve_anthropic_token(model=MODELS[0]) == "test-paid-key"


def test_first_oauth_429_rotates_without_an_extra_failed_request(auth_home):
    from agent.agent_runtime_helpers import recover_with_credential_pool
    runtime = resolve_runtime_provider(requested="anthropic", target_model=MODELS[0])
    agent = SimpleNamespace(provider="anthropic", model=MODELS[0], api_mode="anthropic_messages",
                            base_url="https://api.anthropic.com", api_key=runtime["api_key"],
                            _anthropic_api_key=runtime["api_key"], _credential_pool=runtime["credential_pool"],
                            _credential_pool_entry_id="subscription", _swap_credential=Mock(return_value=True))
    recovered, _ = recover_with_credential_pool(agent, status_code=429, has_retried_429=False,
                                               error_context={"reset_at": time.time() + 120})
    assert recovered
    assert agent._swap_credential.call_args.args[0].id == "paid"


def test_pkce_singleton_not_hidden_by_api_key(auth_home, monkeypatch):
    (auth_home / "auth.json").write_text(json.dumps({"version": 1, "providers": {}}))
    (auth_home / ".anthropic_oauth.json").write_text(json.dumps({
        "accessToken": "cc-test-singleton", "expiresAt": int(time.time() * 1000) + 3_600_000,
    }))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-env-paid-key")
    assert resolve_runtime_provider(requested="anthropic", target_model=MODELS[0])["api_key"] == "cc-test-singleton"


def test_explicit_api_key_remains_authoritative(auth_home):
    runtime = resolve_runtime_provider(requested="anthropic", target_model=MODELS[0],
                                       explicit_api_key="test-explicit-key")
    assert runtime["api_key"] == "test-explicit-key"


def test_expired_cooldown_returns_to_oauth(auth_home, monkeypatch):
    pool = load_pool("anthropic")
    now = time.time()
    pool.mark_exhausted_and_rotate(status_code=429, credential_id="subscription", model=MODELS[0],
                                   error_context={"reset_at": now + 120})
    monkeypatch.setattr(time, "time", lambda: now + 121)
    assert resolve_runtime_provider(requested="anthropic", target_model=MODELS[0])["api_key"] == "cc-test-subscription"


def test_all_exhausted_does_not_resurrect_env_key(auth_home, monkeypatch):
    from agent.anthropic_credentials import resolve_anthropic_token
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-env-paid-key")
    pool = load_pool("anthropic")
    for entry in pool.entries():
        pool.mark_exhausted_and_rotate(status_code=429, credential_id=entry.id, model=MODELS[0],
                                       error_context={"reset_at": time.time() + 120})
    assert resolve_anthropic_token(model=MODELS[0]) is None


def test_auxiliary_uses_oauth_then_paid_key(auth_home):
    import httpx
    import anthropic
    from agent.auxiliary_client import _select_pool_entry, _recover_provider_pool
    _, initial = _select_pool_entry("anthropic")
    assert initial.id == "subscription"
    response = httpx.Response(429, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    error = anthropic.RateLimitError("rate limited", response=response, body={"type": "rate_limit_error"})
    assert _recover_provider_pool("anthropic", error, failed_api_key=initial.runtime_api_key)
    _, fallback = _select_pool_entry("anthropic")
    assert fallback.id == "paid"


def test_real_sdk_wire_switches_bearer_to_api_key(auth_home, monkeypatch):
    import httpx
    import anthropic
    from types import MethodType
    from agent.client_lifecycle import ClientLifecycleMixin
    from agent.anthropic_adapter import build_anthropic_client
    from agent.anthropic_credentials import anthropic_route_is_oauth
    from agent.agent_runtime_helpers import recover_with_credential_pool

    seen = []

    def respond(request):
        seen.append(request.headers)
        if len(seen) == 1:
            return httpx.Response(429, json={"type": "error", "error": {"type": "rate_limit_error", "message": "limited"}})
        return httpx.Response(200, json={"id": "test", "type": "message", "role": "assistant",
                                       "model": MODELS[0], "content": [{"type": "text", "text": "OK"}],
                                       "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}})

    sdk_init = anthropic.Anthropic.__init__

    def init_with_transport(self, **kwargs):
        kwargs["http_client"] = httpx.Client(transport=httpx.MockTransport(respond))
        sdk_init(self, **kwargs)

    monkeypatch.setattr(anthropic.Anthropic, "__init__", init_with_transport)
    runtime = resolve_runtime_provider(requested="anthropic", target_model=MODELS[0])
    agent = SimpleNamespace(provider="anthropic", model=MODELS[0], api_mode="anthropic_messages",
                            base_url="https://api.anthropic.com", api_key=runtime["api_key"],
                            _credential_pool=runtime["credential_pool"], _credential_pool_entry_id="subscription",
                            _anthropic_client=build_anthropic_client(runtime["api_key"]),
                            _build_direct_anthropic_client=build_anthropic_client,
                            _anthropic_oauth_flag=lambda key: anthropic_route_is_oauth("https://api.anthropic.com", key))
    agent._swap_credential = MethodType(ClientLifecycleMixin._swap_credential, agent)
    kwargs = {"model": MODELS[0], "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}
    with pytest.raises(anthropic.RateLimitError):
        agent._anthropic_client.messages.create(**kwargs)
    recovered, _ = recover_with_credential_pool(agent, status_code=429, has_retried_429=False)
    assert recovered
    assert agent._anthropic_client.messages.create(**kwargs).content[0].text == "OK"
    agent._anthropic_client.close()
    assert seen[0]["authorization"] == "Bearer cc-test-subscription"
    assert "x-api-key" not in seen[0]
    assert seen[1]["x-api-key"] == "test-paid-key"
    assert "authorization" not in seen[1]
    assert "oauth-2025-04-20" not in seen[1].get("anthropic-beta", "")
    assert not agent._is_anthropic_oauth
