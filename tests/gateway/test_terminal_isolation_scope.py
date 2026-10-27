"""``_terminal_isolation_scope``: a routed turn's TERMINAL_* come from the profile's installed scope, never a
scaffolded or missing home, and a session's frozen policy still wins over it."""

import pytest

from gateway.run import _terminal_isolation_scope
from tools.terminal_scope import reset_terminal_scope, set_terminal_scope
from tools.terminal_tool_config import _tenv


def test_missing_profile_home_is_entered_without_scaffolding(tmp_path):
    archived = tmp_path / "profiles" / "gone"

    with _terminal_isolation_scope(archived):
        pass

    assert not archived.exists()


def test_installed_scope_values_are_what_the_turn_reads(tmp_path):
    home = tmp_path / "profiles" / "bee"
    home.mkdir(parents=True)
    token = set_terminal_scope({"TERMINAL_ENV": "local", "TERMINAL_CWD": "/work/bee"})
    try:
        with _terminal_isolation_scope(home):
            assert _tenv("TERMINAL_CWD") == "/work/bee"
            assert _tenv("TERMINAL_ENV") == "local"
    finally:
        reset_terminal_scope(token)


def test_entering_the_scope_does_not_scaffold_the_profile_home(tmp_path):
    home = tmp_path / "profiles" / "bee"
    home.mkdir(parents=True)
    token = set_terminal_scope({"TERMINAL_ENV": "local"})
    try:
        with _terminal_isolation_scope(home):
            pass
    finally:
        reset_terminal_scope(token)

    assert list(home.iterdir()) == []


def test_runner_initialises_its_tenant_supervisor():
    import inspect

    from gateway.run import GatewayRunner

    assert "self._tenant_supervisor = TenantSupervisor.from_gateway_config" in inspect.getsource(GatewayRunner.__init__)
