"""``/model`` onto a Claude model whose credential is benched by a per-model 429 cooldown.

A model-scoped cooldown is not a missing credential: the switch must succeed with a warning (the
user can queue a prompt past the reset with ``/wait``) instead of failing as "not connected".
Real ``agent.credential_pool`` against a real temp auth store; only the catalog probe is stubbed.
"""
import json

import pytest

KEY = "sk-ant-api03-synthetic-test-key-0000"
MODEL = "claude-sonnet-4-5"


@pytest.fixture
def anthropic_home(tmp_path, monkeypatch):
    root = tmp_path / "hermes-root"
    root.mkdir()
    (tmp_path / "fakehome").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "fakehome"))
    for var in ("ANTHROPIC_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(root))
    import hermes_constants
    hermes_constants._default_hermes_root_memo = None  # type: ignore[attr-defined]
    monkeypatch.setattr(
        "hermes_cli.models_validate.validate_requested_model",
        lambda *a, **k: {"accepted": True, "persist": True, "recognized": True, "message": ""},
    )
    return root


def _seed_pool(root):
    (root / "auth.json").write_text(json.dumps({"credential_pool": {"anthropic": [{
        "id": "seat", "label": "seat", "auth_type": "api_key", "priority": 0,
        "source": "manual", "access_token": KEY,
    }]}}))


def _switch():
    from hermes_cli.model_switch import switch_model
    return switch_model(raw_input=MODEL, current_provider="openrouter", current_model="gpt-4",
                        current_base_url="https://openrouter.ai/api/v1", explicit_provider="anthropic")


@pytest.mark.parametrize("env_key", [False, True], ids=["pooled-key", "env-key"])
def test_rate_limited_model_switches_with_warning(anthropic_home, monkeypatch, env_key):
    from agent.credential_pool import load_pool
    _seed_pool(anthropic_home)
    if env_key:
        monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    load_pool("anthropic").mark_exhausted_and_rotate(
        status_code=429, error_context={"message": "rate limit"}, api_key_hint=KEY,
        failure_reason="rate_limit", model=MODEL,
    )

    result = _switch()

    assert result.success, result.error_message
    assert result.api_key == KEY
    assert "rate-limited" in result.warning_message and "/wait" in result.warning_message


def test_missing_credentials_still_fail(anthropic_home):
    result = _switch()

    assert not result.success
    assert "not connected" in result.error_message
