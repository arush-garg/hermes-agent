"""Gateway boot activates the store's current tool versions on PATH."""

from __future__ import annotations

import pm
from hermes_cli import gateway


def test_gateway_start_activates_current_store_tools(monkeypatch):
    calls = []
    monkeypatch.setattr(pm, "activate", lambda: calls.append(True) or [])

    gateway._activate_current_store_tools()

    assert calls == [True]


def test_activation_failure_never_blocks_gateway_start(monkeypatch):
    def boom():
        raise RuntimeError("store unreadable")

    monkeypatch.setattr(pm, "activate", boom)

    gateway._activate_current_store_tools()  # must not raise
