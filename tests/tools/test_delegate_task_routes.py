"""Per-child route selection must never splice another provider into the parent route."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools.delegate_tool import DELEGATE_TASK_SCHEMA, _build_children, _resolve_task_routes
from tools.delegate_tool_config import _resolve_child_runtime


PARENT = SimpleNamespace(
    model="parent-model", provider="openrouter", base_url="https://parent.example/v1",
    api_key="parent-key", api_mode="chat_completions", acp_command=None, acp_args=[],
    request_overrides={}, _fallback_chain=None,
)
DEFAULT = {
    "model": "configured-model", "provider": "openrouter", "base_url": "https://configured.example/v1",
    "api_key": "configured-key", "api_mode": "chat_completions", "request_overrides": {},
}
CONFIG = {
    "model": "configured-model", "provider": "openrouter", "base_url": "https://configured.example/v1",
    "api_key": "configured-key", "request_overrides": {"parent_filter": True},
    "fallback_providers": [{"provider": "openrouter", "model": "fallback"}],
}


def test_schema_advertises_optional_routes_and_default_behavior():
    fields = DELEGATE_TASK_SCHEMA["parameters"]["properties"]["tasks"]["items"]
    assert {"goal", "model", "provider"} <= fields["properties"].keys()
    assert "model" not in fields["required"]
    assert "provider" not in fields["required"]


def test_default_and_model_only_pin_keep_configured_provider():
    tasks = [{"goal": "default"}, {"goal": "pin", "model": "chosen-model"}]
    creds, cfgs = _resolve_task_routes(tasks, CONFIG, PARENT, None, DEFAULT)
    assert creds[0] is DEFAULT
    assert cfgs[0] is CONFIG
    assert cfgs[1]["provider"] == "openrouter"
    assert cfgs[1]["base_url"] == CONFIG["base_url"]
    assert creds[1]["model"] == "chosen-model"
    assert creds[1]["provider"] == "custom"  # configured direct endpoint retains its URL heuristic


def test_provider_pin_resolves_separate_bundle_and_default_model():
    selected = {
        "model": "provider-default", "provider": "other", "base_url": "https://other.example/v1",
        "api_key": "other-key", "api_mode": "chat_completions", "request_overrides": {},
    }
    with patch("tools.delegate_tool._resolve_delegation_credentials", return_value=selected) as resolver:
        creds, cfgs = _resolve_task_routes(
            [{"goal": "one"}, {"goal": "two", "provider": "other"}], CONFIG, PARENT, None, DEFAULT,
        )
    assert creds == [DEFAULT, selected]
    sent_cfg = resolver.call_args.args[0]
    assert sent_cfg["provider"] == "other"
    assert all(key not in sent_cfg for key in (
        "model", "base_url", "api_key", "api_mode", "command", "args", "request_overrides",
    ))
    assert sent_cfg["fallback_providers"] == []
    assert cfgs[1] == sent_cfg
    assert CONFIG["model"] == "configured-model"  # no mutation


@pytest.mark.parametrize("field,value", [("provider", ""), ("model", 3), ("provider", [])])
def test_invalid_route_rejected_before_children(field, value):
    with pytest.raises(ValueError, match=f"Task 2: {field}"):
        _resolve_task_routes([{"goal": "default"}, {"goal": "bad", field: value}], CONFIG, PARENT, None, DEFAULT)


def test_internal_route_cannot_be_overridden():
    with pytest.raises(ValueError, match="internal routing"):
        _resolve_task_routes([{"goal": "pin", "model": "other"}], CONFIG, PARENT, CONFIG, DEFAULT)


def test_provider_without_model_fails_loudly():
    with patch("tools.delegate_tool._resolve_delegation_credentials", return_value={**DEFAULT, "model": None}):
        with pytest.raises(ValueError, match="no default model"):
            _resolve_task_routes([{"goal": "pin", "provider": "other"}], CONFIG, PARENT, None, DEFAULT)


def test_build_children_receives_distinct_credentials_and_route_policy():
    selected = {**DEFAULT, "provider": "other", "model": "chosen", "base_url": "https://other.example/v1", "api_key": "other-key"}
    calls = []

    def build(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace()

    with patch("tools.delegate_tool_results._build_child_preserving_parent_tools", side_effect=build), \
         patch("tools.delegate_tool._build_child_preserving_parent_tools", side_effect=build):
        children, err = _build_children(
            [{"goal": "a"}, {"goal": "b"}], [None, None],
            {**DEFAULT, "_per_task": [DEFAULT, selected]}, top_role="leaf", max_iterations=2,
            parent_agent=PARENT, routing_cfg={"_per_task": [CONFIG, {"provider": "other"}]},
            live_deleg_id=None, live_writers=[None, None],
        )
    assert err is None
    assert len(children) == 2
    assert calls[0]["model"] == "configured-model"
    assert calls[1]["model"] == "chosen"
    assert calls[0]["override_api_key"] == "configured-key"
    assert calls[1]["override_api_key"] == "other-key"
    assert calls[1]["override_base_url"] == "https://other.example/v1"
    assert calls[1]["routing_cfg"] == {"provider": "other"}


def test_model_only_task_pin_disables_configured_fallback_chain():
    creds, cfgs = _resolve_task_routes(
        [{"goal": "pin", "model": "different-model"}], CONFIG, PARENT, None, DEFAULT,
    )
    assert creds[0]["model"] == "different-model"
    assert cfgs[0]["fallback_providers"] == []


def test_model_only_pin_disables_parent_fallback_chain():
    parent = SimpleNamespace(**{**vars(PARENT), "_fallback_chain": [{"provider": "openrouter", "model": "fallback"}]})
    selected = _resolve_child_runtime(
        parent, {}, parent.api_key, model="different-model", override_provider=None,
        override_base_url=None, override_api_key=None, override_api_mode=None,
        override_acp_command=None, override_acp_args=None, routing_cfg={},
    )
    assert selected["fallback_model"] is None


def test_live_manifest_keeps_each_task_route(tmp_path, monkeypatch):
    import json
    from tools import delegation_live_log

    monkeypatch.setattr(delegation_live_log, "live_transcript_root", lambda: tmp_path)
    (tmp_path / "deleg_test").mkdir()
    delegation_live_log._write_manifest(
        "deleg_test", [{"goal": "a"}, {"goal": "b"}], ["a.log", "b.log"],
        task_routes=[{"provider": "openrouter", "model": "first"},
                     {"provider": "other", "model": "second"}],
    )
    manifest = json.loads((tmp_path / "deleg_test" / "manifest.json").read_text())
    assert [(t["provider"], t["model"]) for t in manifest["tasks"]] == [
        ("openrouter", "first"), ("other", "second"),
    ]


def test_runtime_does_not_inherit_parent_endpoint_or_acp_for_other_provider():
    parent = SimpleNamespace(**vars(PARENT), requested_provider="openrouter")
    parent.acp_command = "parent-command"
    selected = _resolve_child_runtime(
        parent, {}, parent.api_key, model="chosen", override_provider="other",
        override_base_url="https://other.example/v1", override_api_key="other-key",
        override_api_mode="chat_completions", override_acp_command=None, override_acp_args=None,
        routing_cfg={"provider": "other"},
    )
    assert selected["provider"] == "other"
    assert selected["base_url"] == "https://other.example/v1"
    assert selected["api_key"] == "other-key"
    assert selected["acp_command"] is None
