"""Default delegation strategy (``delegation.backend: default``).

Every ``delegate_task`` task takes the route the ``delegation`` config section describes: a
``delegation.base_url`` endpoint, a ``delegation.provider`` pin, ``delegation.model`` and
``delegation.reasoning_effort``, or, when those are empty, the parent's own route. It adds no
schema parameters. Bundled ``kind: backend``, so it is always loaded; other strategies are plugins
of the same kind (``agent/delegation_provider.py``).
"""

from __future__ import annotations

from typing import Any, Mapping

from agent.delegation_provider import DelegationProvider


class DefaultDelegationProvider(DelegationProvider):
    """Route every task through the ``delegation`` section."""

    @property
    def name(self) -> str:
        return "default"

    @property
    def display_name(self) -> str:
        return "Default"

    @property
    def summary(self) -> str:
        return "Every subagent uses delegation.model / delegation.provider, else the parent's model."

    def route_task(self, task: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
        # The delegation section has no speed tier: an unknown key there never becomes one.
        route = {k: v for k, v in config.items() if k != "service_tier"}
        for field in ("model", "provider"):
            pin = task.get(field)
            if pin is not None and (not isinstance(pin, str) or not pin.strip()):
                raise ValueError(f"{field} must be a nonempty string")
        if provider := task.get("provider"):
            # A different provider owns its own endpoint, credentials, transport and default model.
            for key in ("base_url", "api_key", "api_mode", "command", "args",
                        "request_overrides", "fallback_providers", "model"):
                route.pop(key, None)
            route["provider"] = provider.strip()
            route["fallback_providers"] = []
        if model := task.get("model"):
            route["model"] = model.strip()
            route["fallback_providers"] = []
        return route


def register(ctx) -> None:
    ctx.register_delegation_provider(DefaultDelegationProvider())
