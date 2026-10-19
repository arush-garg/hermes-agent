"""Slash-command parity: COMMAND_REGISTRY vs the classic CLI dispatch table vs the TUI routes.

Every registered command must resolve to a handler on BOTH surfaces, or be listed in
``SURFACE_SPECIFIC`` with a reason. TUI routes, in slash.exec/dispatch order:

  ink     — handled client-side by an Ink command module (ui-tui/src/app/slash/commands/*.ts)
            or createSlashHandler's /keep pre-dispatch
  live    — answered from the live gateway session (methods_slash._LIVE_SLASH_OUTPUT / /context)
  builtin — command.dispatch built-in (methods_tools._SLASH_BUILTINS; _PENDING_INPUT_COMMANDS)
  worker  — slash.exec → tui_gateway.slash_worker → HermesCLI.process_command (the CLI handler)

The worker is a separate HermesCLI that resumes the session WITHOUT an agent and has no reader
for ``_pending_input`` and no usable stdin, so a CLI handler that needs any of those must not
be worker-routed. Those invariants are checked statically against the handler source.
"""

from __future__ import annotations

import ast
import inspect
import re
import textwrap
from pathlib import Path

import pytest

import cli
from hermes_cli.commands import COMMAND_REGISTRY

ROOT = Path(__file__).resolve().parents[2]
HermesCLI = cli.HermesCLI

# Commands that intentionally exist on only one surface. name → reason.
SURFACE_SPECIFIC = {
    # Messaging-gateway only (Telegram/Discord/...); hidden from the TUI catalog (gateway_only).
    "start": "gateway: platform onboarding greeting",
    "topic": "gateway: per-thread topic binding",
    "pause": "gateway: pause a platform chat",
    "approve": "gateway: chat-reply approval (TUI uses the approval overlay)",
    "deny": "gateway: chat-reply denial (TUI uses the approval overlay)",
    "sethome": "gateway: set the platform home channel",
    "commands": "gateway: paginated command list (TUI has /help + completion)",
    "restart": "gateway: restart the messaging gateway process",
    "platform": "gateway: per-platform settings",
}

# Worker-routed handlers that touch self.agent but degrade correctly without one.
AGENT_OPTIONAL_WORKER = {
    "memory": "falls back to the on-disk memory store when there is no live agent (#46783)",
    "config": "only prefers the live agent's API key for display; falls back to the resolved key",
}


def _ink_names() -> set[str]:
    names: set[str] = set()
    for f in (ROOT / "ui-tui/src/app/slash/commands").glob("*.ts"):
        src = f.read_text(encoding="utf-8")
        names.update(re.findall(r"name:\s*'([\w-]+)'", src))
        for group in re.findall(r"aliases:\s*\[([^\]]*)\]", src):
            names.update(re.findall(r"'([\w-]+)'", group))
    handler = (ROOT / "ui-tui/src/app/createSlashHandler.ts").read_text(encoding="utf-8")
    names.update(re.findall(r"parsed\.name === '([\w-]+)'", handler))
    return names


@pytest.fixture(scope="module")
def routes():
    from tui_gateway import server
    from tui_gateway import methods_tools as mt
    return {
        "ink": _ink_names(),
        "live": set(server._LIVE_SLASH_OUTPUT) | {"context", "model"},
        "builtin": set(mt._SLASH_BUILTINS),
        "pending": set(server._PENDING_INPUT_COMMANDS),
        "hidden": set(server._TUI_HIDDEN),
    }


def _names(cmd) -> list[str]:
    return [cmd.name, *cmd.aliases]


def _tui_route(cmd, r) -> str:
    names = _names(cmd)
    for route in ("ink", "live", "builtin"):
        if any(n in r[route] for n in names):
            return route
    return "worker"


def _handler_src(cmd) -> str:
    entry = HermesCLI._slash_handler(cmd.name)
    fn = getattr(HermesCLI, entry[0])
    return textwrap.dedent(inspect.getsource(fn))


def test_surface_specific_entries_are_registered_and_reasoned():
    registered = {c.name for c in COMMAND_REGISTRY}
    for name, reason in SURFACE_SPECIFIC.items():
        assert name in registered, f"stale SURFACE_SPECIFIC entry: /{name}"
        assert reason.strip()


def test_every_command_has_a_cli_handler_or_is_surface_specific():
    missing = [c.name for c in COMMAND_REGISTRY
               if c.name not in SURFACE_SPECIFIC and HermesCLI._slash_handler(c.name) is None]
    assert not missing, f"registered with no classic-CLI handler: {missing}"
    for c in COMMAND_REGISTRY:
        if c.gateway_only:
            assert c.name in SURFACE_SPECIFIC, f"/{c.name} is gateway_only but not declared surface-specific"


def test_cli_dispatch_table_only_names_registered_commands():
    from hermes_cli.commands import resolve_command
    for key, (method, _) in HermesCLI._SLASH_DISPATCH.items():
        assert resolve_command(key) is not None, f"_SLASH_DISPATCH entry /{key} is not in COMMAND_REGISTRY"
        assert callable(getattr(HermesCLI, method, None)), f"/{key} → missing HermesCLI.{method}"


def test_every_command_has_a_tui_route(routes):
    """Every non-surface-specific command reaches a handler in the TUI: Ink-local, live, built-in,
    or the worker — and a worker route is only valid when the CLI handler exists."""
    for c in COMMAND_REGISTRY:
        if c.name in SURFACE_SPECIFIC:
            continue
        route = _tui_route(c, routes)
        if route == "worker":
            assert HermesCLI._slash_handler(c.name) is not None, f"/{c.name} worker-routed with no CLI handler"
        assert c.name not in routes["hidden"], f"/{c.name} hidden from the TUI but not surface-specific"


def test_tui_dispatch_tables_only_name_registered_or_tui_extra_commands(routes):
    from hermes_cli.commands import resolve_command
    from tui_gateway import server
    extra = {name.lstrip("/") for name, *_ in server._TUI_EXTRA}
    for key in routes["builtin"] | routes["pending"]:
        assert resolve_command(key) is not None, f"gateway built-in /{key} is not registered"
    live_extras = {"models", "rename", "effort"}  # friendly redirects for common non-commands
    for key in routes["live"] - live_extras:
        assert resolve_command(key) is not None or key in extra, f"live slash /{key} is not registered"


def test_pending_input_commands_never_reach_the_worker(routes):
    """A CLI handler that queues onto _pending_input loses the prompt in the worker (no reader)."""
    for c in COMMAND_REGISTRY:
        if c.name in SURFACE_SPECIFIC or _tui_route(c, routes) != "worker":
            continue
        src = _handler_src(c)
        assert "_pending_input" not in src and "_queue_prompt_turn" not in src and "_kick_goal" not in src, (
            f"/{c.name} queues a turn via _pending_input but is worker-routed; add it to "
            f"_PENDING_INPUT_COMMANDS with a command.dispatch built-in")


def test_live_agent_commands_never_reach_the_worker(routes):
    """The worker builds no agent, so a handler reading self.agent / conversation_history there
    always answers "send a message first" — route it through the live session instead."""
    pat = re.compile(r"self\.agent\b|getattr\(self,\s*[\"']agent[\"']|conversation_history")
    for c in COMMAND_REGISTRY:
        if c.name in SURFACE_SPECIFIC or c.name in AGENT_OPTIONAL_WORKER or _tui_route(c, routes) != "worker":
            continue
        assert not pat.search(_handler_src(c)), f"/{c.name} needs the live agent but is worker-routed"


def test_headless_worker_never_reads_stdin():
    """stdin is the worker's JSON-RPC pipe: a prompt there swallows the next request."""
    c = HermesCLI.__new__(HermesCLI)
    c._headless_slash_worker = True
    c._app = None
    import builtins
    called = []
    orig = builtins.input
    builtins.input = lambda *a: called.append(a) or "SWALLOWED"
    try:
        assert c._prompt_text_input("Describe your pet: ") is None
    finally:
        builtins.input = orig
    assert not called


def test_parity_shared_live_bodies_are_used_by_both_surfaces():
    from tui_gateway import methods_slash
    cli_src = inspect.getsource(HermesCLI._show_context_breakdown) + inspect.getsource(HermesCLI._handle_refine_command)
    gw_src = inspect.getsource(methods_slash)
    for fn in ("context_breakdown_lines", "start_refine"):
        assert fn in cli_src and fn in gw_src, fn
    assert ast.parse(gw_src)


def _live_session(agent, history):
    import threading
    return {"agent": agent, "session_key": "parity-key", "history": history,
            "history_lock": threading.Lock(), "running": False, "cwd": ""}


def test_tui_context_and_refine_use_the_live_agent(monkeypatch):
    """Before: both went to the agentless worker and always said "send a message first"."""
    from unittest.mock import MagicMock, patch
    from tui_gateway import server
    agent = MagicMock(model="m-1", valid_tool_names={"skill_manage"})
    session = _live_session(agent, [{"role": "user", "content": "hi"}])
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda s: False)
    with patch("hermes_cli.live_slash_core.context_breakdown_lines", return_value=["GRID", "ROW"]) as ctx:
        out = server._live_slash_command_output("sid", session, "context", "all")
    assert out == "GRID\nROW"
    assert ctx.call_args.args[:3] == (agent, session["history"], "all")

    out = server._live_slash_command_output("sid", session, "refine", "naming")
    agent._spawn_background_review.assert_called_once()
    kw = agent._spawn_background_review.call_args.kwargs
    assert kw["focus"] == "naming" and kw["explicit"] is True and kw["review_skills"] is True
    assert kw["messages_snapshot"] == session["history"]
    assert "(focus: naming)" in out

    # No live agent: /refine says so; /context falls through (None) to the existing route.
    bare = _live_session(None, [])
    assert server._live_slash_command_output("sid", bare, "refine", "") == "Nothing to refine yet — send a message first."
    assert server._live_slash_command_output("sid", bare, "context", "") is None


def test_tui_palette_answers_instead_of_opening_a_headless_modal():
    from tui_gateway import server
    out = server._live_slash_command_output("sid", None, "palette", "")
    assert out and "classic-CLI only" in out


def test_cli_context_and_refine_run_the_shared_bodies(capsys):
    from unittest.mock import MagicMock, patch
    c = HermesCLI.__new__(HermesCLI)
    c.agent = MagicMock(model="m-1", valid_tool_names=set())
    c.conversation_history = [{"role": "user", "content": "hi"}]
    c.model = "m-1"
    with patch("hermes_cli.live_slash_core.context_breakdown_lines", return_value=["GRID"]) as ctx:
        c._show_context_breakdown("/context all")
    assert ctx.call_args.args[:3] == (c.agent, c.conversation_history, "all")
    assert "GRID" in capsys.readouterr().out
    with patch.object(cli, "_cprint", lambda t: print(t)):
        c._handle_refine_command("/refine naming")
    assert c.agent._spawn_background_review.call_args.kwargs["focus"] == "naming"
    assert c.agent._spawn_background_review.call_args.kwargs["review_skills"] is False
    assert "(focus: naming)" in capsys.readouterr().out
