"""Route precedence under a non-default delegation backend (e.g. advanced-delegation's model_map):

1. an explicit per-task ``model``/``provider`` pin wins exactly (the default backend's pin rules);
2. else the route the task names (the backend's own parameter);
3. else the backend's default route.
An unknown route name is refused; a route's fallback chain is its own, else ``delegation.fallback_providers``.
"""
import pytest

from agent.delegation_provider import DelegationProvider, DelegationRouteError
from tools.delegate_tool_routes import _route_one, _with_inherited_fallback

OPTIONS = {
    "fast": {"provider": "openai-codex", "model": "gpt-6-luna"},
    "standard": {"provider": "anthropic", "model": "claude-sonnet-5-5"},
    "security": {"provider": "omniroute", "model": "grok-cli/grok-4.7",
                 "fallback_providers": [{"provider": "nvidia", "model": "own-fb"}]},
}
CONFIG = {
    "backend": "fake_map", "model": "main", "provider": "omniroute", "base_url": "http://localhost:20128/v1",
    "fallback_providers": [{"provider": "claude-cli", "model": "claude-sonnet-5-5"}],
}


class FakeModelMap(DelegationProvider):
    """Same contract as advanced-delegation's model_map: one ``option`` enum, ``default`` route, pins ignored."""

    @property
    def name(self):
        return "fake_map"

    def task_parameters(self, config):
        return {"option": {"type": "string", "enum": list(OPTIONS)}}

    def route_task(self, task, config):
        chosen = task.get("option") or "standard"
        return {**OPTIONS[chosen], "label": chosen}


def _route(task):
    provider = FakeModelMap()
    return _route_one(provider, provider.task_parameters(CONFIG), 0, task, CONFIG)


def test_model_and_provider_pin_wins_over_backend():
    route = _route({"goal": "g", "model": "gpt-5.6-terra", "provider": "openai-codex"})
    assert (route["model"], route["provider"]) == ("gpt-5.6-terra", "openai-codex")
    assert route["fallback_providers"] == []  # a pin runs exactly that model
    assert "base_url" not in route  # never paired with the delegation section's endpoint


def test_pin_wins_even_when_an_option_is_also_named():
    route = _route({"goal": "g", "model": "claude-opus-5-5", "provider": "anthropic", "option": "fast"})
    assert (route["model"], route["provider"]) == ("claude-opus-5-5", "anthropic")


def test_model_only_pin_wins():
    route = _route({"goal": "g", "model": "pinned-model"})
    assert route["model"] == "pinned-model" and route["fallback_providers"] == []


def test_invalid_pin_is_refused():
    with pytest.raises(ValueError, match="model"):
        _route({"goal": "g", "model": "  "})


def test_named_route_is_picked():
    route = _route({"goal": "g", "option": "fast"})
    assert (route["provider"], route["model"], route["label"]) == ("openai-codex", "gpt-6-luna", "fast")


def test_default_route_without_pin_or_option():
    route = _route({"goal": "g"})
    assert (route["model"], route["label"]) == ("claude-sonnet-5-5", "standard")


def test_unknown_route_is_refused():
    with pytest.raises(DelegationRouteError, match="option"):
        _route({"goal": "g", "option": "nope"})


def test_per_route_fallback_inherits_or_keeps_own():
    assert _with_inherited_fallback(_route({"goal": "g", "option": "fast"}), CONFIG)["fallback_providers"] \
        == CONFIG["fallback_providers"]
    assert _with_inherited_fallback(_route({"goal": "g", "option": "security"}), CONFIG)["fallback_providers"] \
        == [{"provider": "nvidia", "model": "own-fb"}]
    assert _with_inherited_fallback(_route({"goal": "g", "model": "m", "provider": "p"}), CONFIG)[
        "fallback_providers"] == []
