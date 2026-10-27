"""``hermes kickbacks <sub>`` parses and dispatches into the external plugin's ``cli.py``."""

from __future__ import annotations

import sys
import types

import pytest

from hermes_cli import main as hermes_main

_SUBCOMMANDS = ("login", "logout", "status", "config", "enable", "disable", "demo", "live", "ads")


def _parse(argv):
    built = hermes_main._build_cli_parser()
    parser = built[0] if isinstance(built, tuple) else built
    return parser.parse_args(argv)


@pytest.fixture
def plugin(monkeypatch):
    calls = []
    stub = types.SimpleNamespace(
        cmd_login=lambda: calls.append("login"), cmd_logout=lambda: calls.append("logout"),
        cmd_status=lambda: calls.append("status"), cmd_config=lambda: calls.append("config"),
        cmd_demo=lambda: calls.append("demo"), cmd_live=lambda: calls.append("live"),
        cmd_ads_list=lambda: calls.append("ads list"), cmd_help=lambda: calls.append("help"),
        set_config=lambda key, value: calls.append(("set_config", key, value)),
    )
    monkeypatch.setitem(sys.modules, "kickbacks_cli_module", stub)
    return calls


@pytest.mark.parametrize("sub", _SUBCOMMANDS)
def test_every_subcommand_is_registered(sub):
    args = _parse(["kickbacks", sub])

    assert args.kickbacks_command == sub


@pytest.mark.parametrize("sub", ["login", "logout", "status", "config", "demo", "live"])
def test_simple_subcommands_call_the_plugin_function(plugin, sub, capsys):
    args = _parse(["kickbacks", sub])

    args.func(args)

    assert plugin == [sub]


@pytest.mark.parametrize(("sub", "enabled"), [("enable", True), ("disable", False)])
def test_enable_and_disable_write_the_enabled_flag(plugin, sub, enabled, capsys):
    args = _parse(["kickbacks", sub])

    args.func(args)

    assert plugin == [("set_config", "enabled", enabled)]


def test_ads_list_and_bare_command_dispatch(plugin):
    listing = _parse(["kickbacks", "ads", "list"])
    listing.func(listing)
    bare = _parse(["kickbacks"])
    bare.func(bare)

    assert plugin == ["ads list", "help"]
