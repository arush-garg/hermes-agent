"""Keep provider-registry state from leaking between tests in this package.

Several tests clear or swap ``providers._REGISTRY`` / ``_discovered`` (forcing re-discovery) and some
undo only part of it, leaving a later test with ``_discovered=True`` over an empty registry.
"""

from __future__ import annotations

import sys

import pytest

_PLUGIN_MODULE_PREFIXES = ("plugins.model_providers", "_hermes_user_provider")


@pytest.fixture(autouse=True)
def _restore_provider_registry_state():
    import providers

    registry, aliases, sources = dict(providers._REGISTRY), dict(providers._ALIASES), dict(providers._SOURCES)
    discovered, list_cache = providers._discovered, providers._PROVIDER_LIST_CACHE
    layers = dict(providers._HOME_LAYERS)
    plugin_modules = {n: m for n, m in sys.modules.items() if n.startswith(_PLUGIN_MODULE_PREFIXES)}
    yield
    # Tests evict plugin modules to force re-import; the restored registry still holds classes from them.
    for name in [n for n in sys.modules if n.startswith(_PLUGIN_MODULE_PREFIXES) and n not in plugin_modules]:
        del sys.modules[name]
    sys.modules.update(plugin_modules)
    providers._REGISTRY.clear()
    providers._REGISTRY.update(registry)
    providers._ALIASES.clear()
    providers._ALIASES.update(aliases)
    providers._SOURCES.clear()
    providers._SOURCES.update(sources)
    providers._discovered = discovered
    providers._PROVIDER_LIST_CACHE = list_cache
    providers._HOME_LAYERS.clear()
    providers._HOME_LAYERS.update(layers)
    providers._REGISTRY_GENERATION += 1  # invalidate memos keyed on the registry
