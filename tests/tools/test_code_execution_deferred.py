"""execute_code calling opted-in deferred/MCP tools (``code_execution.deferred_tools``).

Behaviour pinned here: the feature is off by default (no helpers, unchanged schema, raw RPC
refused); the resolver's globs, denylist, trust and backend rules; session scoping; and the
end-to-end path through a REAL session kernel with a fake MCP tool in the registry: results,
the separate deferred budget, the shared max_tool_calls budget, kernel keying by allowed set,
and byte-stable schemas.
"""

import json
import os
from contextlib import contextmanager
from unittest.mock import patch

import pytest

os.environ["TERMINAL_ENV"] = "local"

from tools import code_execution_deferred as deferred_mod
from tools.code_execution_deferred import (
    DENIED_PREFIXES, DENIED_TOOLS, DeferredScope, load_deferred_config, resolve_deferred_tools,
    with_deferred_execute_code,
)
from tools.code_execution_rpc import _handle_rpc_request
from tools.code_execution_tool import SANDBOX_ALLOWED_TOOLS, build_execute_code_schema, execute_code
from tools.code_kernel import _KERNELS, shutdown_all_kernels
from tools.registry import registry

BUILTINS = set(SANDBOX_ALLOWED_TOOLS)

FAKE = "mcp__fake__echo"
OTHER = "mcp__fake__other"
FAKE_TOOLSET = "mcp-fake"
SESSION = ["mcp-fake"]
LIMIT_PREFIX = "Deferred tool call limit reached (25). Call to 'mcp__fake__echo' blocked"


@pytest.fixture(autouse=True)
def _local_backend_and_fresh_kernels(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    shutdown_all_kernels()
    yield
    shutdown_all_kernels()


def _schema(name):
    return {"name": name, "description": f"Echo the given text ({name}).",
            "parameters": {"type": "object", "properties": {"text": {"type": "string"}}}}


@pytest.fixture
def fake_tools():
    """Two fake MCP tools in toolset mcp-fake; ``calls`` records every real dispatch."""
    calls = []

    def handler(name):
        def run(args, **_kw):
            calls.append((name, dict(args)))
            return json.dumps({"echo": args.get("text"), "tool": name, "n": len(calls)})
        return run

    for name in (FAKE, OTHER):
        registry.register(name=name, toolset=FAKE_TOOLSET, schema=_schema(name), handler=handler(name))
    yield calls
    for name in (FAKE, OTHER):
        registry.deregister(name)


@contextmanager
def _config(**overrides):
    config = {"mode": "strict", "timeout": 60}
    config.update(overrides)
    with patch("tools.code_execution_tool._load_config", return_value=config):
        yield


def _run(code, **kwargs):
    kwargs.setdefault("enabled_toolsets", SESSION)
    return json.loads(execute_code(code, task_id="deferred-test", **kwargs))


def _rpc(tool, *, allowed=SANDBOX_ALLOWED_TOOLS, deferred=None, counter=None, max_calls=50, log=None):
    seen = []
    result = _handle_rpc_request(
        {"tool": tool, "args": {"text": "x"}}, allowed_tools=allowed, tool_call_counter=counter or [0],
        max_tool_calls=max_calls, dispatch=lambda n, a: seen.append(n) or json.dumps({"ok": n}),
        tool_call_log=log if log is not None else [], call_start=0.0, where="test", deferred=deferred)
    return result, seen


# ---- 1. Off by default ------------------------------------------------------------------------

class TestOffByDefault:
    def test_default_config_has_the_keys_and_resolves_nothing(self):
        from hermes_cli.config_defaults import DEFAULT_CONFIG
        block = DEFAULT_CONFIG["code_execution"]
        assert block["deferred_tools"] == [] and block["max_deferred_tool_calls"] == 25
        with _config():
            assert load_deferred_config() == ((), 25)

    def test_no_helpers_in_the_kernel(self, fake_tools):
        with _config():
            out = _run("from hermes_tools import call_tool")
        assert out["status"] == "error", out
        assert "ImportError" in out["error"] or "cannot import name" in out["error"]

    def test_schema_unchanged(self, fake_tools):
        from model_tools import _clear_tool_defs_cache, get_tool_definitions

        def execute_code_def():
            _clear_tool_defs_cache()
            defs = get_tool_definitions(enabled_toolsets=["code_execution", FAKE_TOOLSET], quiet_mode=True,
                                        skip_tool_search_assembly=True)
            return json.dumps(next(d for d in defs if d["function"]["name"] == "execute_code"), sort_keys=True)

        with _config():
            off = execute_code_def()
        with _config(deferred_tools=["mcp__nothing__*"]):
            on_but_empty = execute_code_def()
        assert off == on_but_empty
        assert "call_tool" not in off

    def test_raw_rpc_to_a_registered_deferred_tool_is_refused(self, fake_tools):
        result, seen = _rpc(FAKE)
        assert json.loads(result)["error"].startswith(f"Tool '{FAKE}' is not available in execute_code.")
        assert seen == []
        with _config():
            out = _run(f"import hermes_tools\nprint(hermes_tools._call({FAKE!r}, {{'text': 'x'}}))")
        assert f"Tool '{FAKE}' is not available in execute_code" in out["output"], out
        assert fake_tools == [] and out["tool_calls_made"] == 0


# ---- 2. Resolver ------------------------------------------------------------------------------

def _resolve(names, patterns, local=True, untrusted=lambda _n: False):
    return resolve_deferred_tools(names, patterns, backend_is_local=local, is_untrusted=untrusted)


class TestResolver:
    NAMES = {"mcp__browseros__act", "mcp__browseros__tabs", "mcp__omega__store", "process_manage",
             "execute_code", "delegate_task", "clarify", "memory", "tool_call", "send_message",
             "connectors__gmail__send", "terminal", "read_file"}

    def test_globs_are_case_sensitive_fnmatch(self):
        assert _resolve(self.NAMES, ["mcp__browseros__*", "process_manage"]) == {
            "mcp__browseros__act", "mcp__browseros__tabs", "process_manage"}
        assert _resolve(self.NAMES, ["MCP__browseros__*"]) == frozenset()
        assert _resolve(self.NAMES, ["mcp__*__store"]) == {"mcp__omega__store"}

    def test_denylist_and_connectors_beat_star(self):
        got = _resolve(self.NAMES, ["*"])
        assert got == {"mcp__browseros__act", "mcp__browseros__tabs", "mcp__omega__store", "process_manage"}
        assert not got & DENIED_TOOLS
        assert "connectors__" in DENIED_PREFIXES
        assert {"execute_code", "delegate_task", "tool_search", "tool_call", "todo_list",
                "manage_connections"} <= DENIED_TOOLS

    def test_sandbox_builtins_are_excluded(self):
        assert _resolve(self.NAMES, ["terminal", "read_file"]) == frozenset()

    def test_untrusted_server_excluded_via_injected_helper(self):
        got = _resolve(self.NAMES, ["mcp__*"], untrusted=lambda n: n.startswith("mcp__browseros__"))
        assert got == {"mcp__omega__store"}

    def test_untrusted_server_excluded_via_trust_table(self, monkeypatch):
        from tools import mcp_tool as core
        from tools.mcp_tool_scope import _server_key
        monkeypatch.setitem(core._mcp_tool_server_names, "mcp__browseros__act", "browseros")
        monkeypatch.setitem(core._server_trust_levels, _server_key("browseros"), "untrusted")
        monkeypatch.setitem(core._server_trust_levels, _server_key("ai-kline"), "untrusted")
        got = resolve_deferred_tools({"mcp__browseros__act", "mcp__ai_kline__quote", "mcp__omega__store"},
                                     ["mcp__*"], backend_is_local=True)
        assert got == {"mcp__omega__store"}

    def test_remote_backend_and_empty_patterns_give_empty(self):
        assert _resolve(self.NAMES, ["*"], local=False) == frozenset()
        assert _resolve(self.NAMES, []) == frozenset()

    @pytest.mark.parametrize("value", ["mcp__*", 5, {"a": 1}, ["mcp__*", 3], True])
    def test_garbage_patterns_fail_closed(self, value):
        with _config(deferred_tools=value):
            assert load_deferred_config()[0] == ()

    @pytest.mark.parametrize(("value", "expected"), [(0, 1), (-4, 1), (999, 200), (7, 7), ("x", 25), (None, 25)])
    def test_budget_is_clamped(self, value, expected):
        with _config(deferred_tools=["mcp__*"], max_deferred_tool_calls=value):
            assert load_deferred_config() == (("mcp__*",), expected)

    def test_remote_backend_resolves_nothing_end_to_end(self, fake_tools):
        with _config(deferred_tools=["mcp__fake__*"]), \
                patch.object(deferred_mod, "backend_is_local", return_value=False):
            assert deferred_mod.build_deferred_scope(SESSION, None) is None
            defs = [{"type": "function", "function": build_execute_code_schema(mode="strict")},
                    {"type": "function", "function": _schema(FAKE)}]
            assert with_deferred_execute_code(defs) is defs


# ---- 3. Session scope -------------------------------------------------------------------------

class TestSessionScope:
    def test_tool_outside_the_session_is_refused(self, fake_tools):
        code = (f"import hermes_tools\nprint(hasattr(hermes_tools, 'call_tool'))\n"
                f"print(hermes_tools._call({FAKE!r}, {{'text': 'x'}}))")
        with _config(deferred_tools=["mcp__fake__*"]):
            out = _run(code, enabled_toolsets=["code_execution"])
        assert out["output"].splitlines()[0] == "False", out
        assert f"Tool '{FAKE}' is not available in execute_code" in out["output"]
        assert fake_tools == []

    def test_handle_function_call_carries_the_session_scope(self, fake_tools):
        """Production wiring: the agent's toolsets reach execute_code through handle_function_call."""
        from model_tools import handle_function_call
        code = f"import hermes_tools\nprint(hermes_tools._call({FAKE!r}, {{'text': 'x'}}))"
        with _config(deferred_tools=["mcp__fake__*"]):
            granted = json.loads(handle_function_call("execute_code", {"code": code}, task_id="deferred-test",
                                                      enabled_toolsets=["code_execution", FAKE_TOOLSET]))
            outside = json.loads(handle_function_call("execute_code", {"code": code}, task_id="deferred-test",
                                                      enabled_toolsets=["code_execution"]))
        assert "'echo': 'x'" in granted["output"], granted
        assert f"Tool '{FAKE}' is not available in execute_code" in outside["output"], outside
        assert len(fake_tools) == 1

    def test_disabled_toolset_is_refused(self, fake_tools):
        with _config(deferred_tools=["mcp__fake__*"]):
            assert deferred_mod.build_deferred_scope(None, [FAKE_TOOLSET]) is None
            scope = deferred_mod.build_deferred_scope(SESSION, None)
        assert scope is not None and scope.names == {FAKE, OTHER}

    def test_scope_refuses_names_it_does_not_hold(self):
        scope = DeferredScope(frozenset({FAKE}), 25, {})
        result, seen = _rpc(OTHER, deferred=scope)
        assert json.loads(result)["error"].startswith(f"Tool '{OTHER}' is not available in execute_code.")
        assert seen == []


# ---- 4. End to end through a real session kernel ----------------------------------------------

class TestRealKernel:
    def test_call_list_describe_and_hooks_see_the_real_name(self, fake_tools):
        hooked = []

        def pre_hook(name, args, **_kw):
            hooked.append(name)
            return None, None

        code = ("import json\nfrom hermes_tools import call_tool, list_deferred_tools, describe_tool\n"
                f"r = call_tool({FAKE!r}, text='hi')\nprint(json.dumps(r))\n"
                "print(json.dumps(list_deferred_tools()))\n"
                f"print(json.dumps(describe_tool({FAKE!r})))\n"
                "print(json.dumps(describe_tool('terminal')))")
        with _config(deferred_tools=["mcp__fake__*"]), \
                patch("hermes_cli.plugins._dispatch_pre_tool_call_hooks", side_effect=pre_hook):
            out = _run(code)
        assert out["status"] == "success", out
        result, names, described, refused = (json.loads(line) for line in out["output"].splitlines()[:4])
        assert result == {"echo": "hi", "tool": FAKE, "n": 1}
        assert names == [FAKE, OTHER]
        assert (described["name"], described["description"]) == (FAKE, f"Echo the given text ({FAKE}).")
        assert described["parameters"]["properties"] == {"text": {"type": "string"}}
        assert "error" in refused
        assert fake_tools == [(FAKE, {"text": "hi"})]
        assert hooked == [FAKE]
        assert out["tool_calls_made"] == 1
        kernel = next(iter(_KERNELS.values()))
        assert [e.get("deferred") for e in kernel.tool_call_log if e["tool"] == FAKE] == [True]

    def test_non_json_and_multiline_results_survive_the_rpc_framing(self):
        """A deferred tool may return plain, multi-line or pretty-printed text; the newline-framed
        RPC must deliver each intact (raw text stays text, JSON is parsed)."""
        shaped = "mcp__fake__shaped"
        replies = {"plain": "just text", "lines": "line one\nline two\n",
                   "pretty": json.dumps({"a": [1, 2]}, indent=2), "compact": json.dumps({"b": 3})}
        registry.register(name=shaped, toolset=FAKE_TOOLSET, schema=_schema(shaped),
                          handler=lambda args, **_kw: replies[args["text"]])
        try:
            code = ("import json\nfrom hermes_tools import call_tool\n"
                    f"print(json.dumps([call_tool({shaped!r}, text=k) for k in "
                    "('plain', 'lines', 'pretty', 'compact')]))")
            with _config(deferred_tools=[shaped]):
                out = _run(code)
        finally:
            registry.deregister(shaped)
        assert out["status"] == "success", out
        assert json.loads(out["output"].splitlines()[0]) == [
            "just text", "line one\nline two\n", {"a": [1, 2]}, {"b": 3}]
        assert out["tool_calls_made"] == 4

    def test_deferred_budget_stops_at_25(self, fake_tools):
        code = ("from hermes_tools import call_tool\nok, err = 0, []\n"
                "for i in range(30):\n"
                f"    r = call_tool({FAKE!r}, text=str(i))\n"
                "    if isinstance(r, dict) and 'error' in r:\n        err.append(r['error'])\n"
                "    else:\n        ok += 1\n"
                "print(ok)\nprint(err[0])\nprint(len(err))")
        with _config(deferred_tools=["mcp__fake__*"], max_deferred_tool_calls=25):
            out = _run(code)
        ok, first_error, errors = out["output"].splitlines()[:3]
        assert (ok, errors) == ("25", "5"), out
        assert first_error.startswith(LIMIT_PREFIX)
        assert len(fake_tools) == 25
        assert out["tool_calls_made"] == 25

    def test_deferred_call_runs_under_the_cells_approval_callback(self):
        """CellAuthority carries the calling thread's approval callback into the deferred handler."""
        from tools.terminal_tool import _get_approval_callback, set_approval_callback
        seen = []

        def handler(args, **_kw):
            seen.append(_get_approval_callback())
            return json.dumps({"ok": True})

        def approve(*_a, **_kw):
            return "once"

        registry.register(name=FAKE, toolset=FAKE_TOOLSET, schema=_schema(FAKE), handler=handler)
        previous = _get_approval_callback()
        set_approval_callback(approve)
        try:
            with _config(deferred_tools=[FAKE]):
                out = _run(f"from hermes_tools import call_tool\nprint(call_tool({FAKE!r}))")
        finally:
            set_approval_callback(previous)
            registry.deregister(FAKE)
        assert out["status"] == "success", out
        assert seen == [approve]

    def test_budget_resets_per_cell(self, fake_tools):
        code = f"from hermes_tools import call_tool\nprint(sum('error' not in call_tool({FAKE!r}) for _ in range(3)))"
        with _config(deferred_tools=["mcp__fake__*"], max_deferred_tool_calls=2):
            first, second = _run(code), _run(code)
        assert first["output"].strip() == second["output"].strip() == "2"
        assert second["kernel"]["reused"] is True


# ---- 5. Budget interplay ----------------------------------------------------------------------

class TestBudgetInterplay:
    def test_deferred_calls_count_toward_max_tool_calls(self, fake_tools):
        code = ("from hermes_tools import call_tool\nerrs = []\n"
                f"for i in range(8):\n    r = call_tool({FAKE!r}, text=str(i))\n"
                "    if 'error' in r: errs.append(r['error'])\nprint(errs[0])")
        with _config(deferred_tools=["mcp__fake__*"], max_tool_calls=5):
            out = _run(code)
        assert out["output"].startswith("Tool call limit reached (5)."), out
        assert len(fake_tools) == 5 and out["tool_calls_made"] == 5

    def test_refusals_are_free_and_builtins_spend_only_the_shared_budget(self):
        scope, counter, log = DeferredScope(frozenset({FAKE}), 1, {}), [0], []
        _rpc(FAKE, deferred=scope, counter=counter, log=log)
        blocked, seen = _rpc(FAKE, deferred=scope, counter=counter, log=log)
        assert json.loads(blocked)["error"].startswith("Deferred tool call limit reached (1). Call to")
        assert seen == [] and counter == [1]
        _, seen = _rpc("read_file", deferred=scope, counter=counter, log=log)
        assert seen == ["read_file"] and counter == [2] and scope.calls == 1
        assert [e.get("deferred", False) for e in log] == [True, False]


# ---- 6. Kernel keying -------------------------------------------------------------------------

class TestKernelKeying:
    def test_different_allowed_sets_get_different_kernels(self, fake_tools):
        code = "from hermes_tools import list_deferred_tools\nprint(list_deferred_tools())"
        with _config(deferred_tools=[FAKE]):
            narrow = _run(code)
        with _config(deferred_tools=["mcp__fake__*"]):
            wide = _run(code)
        assert narrow["output"].strip() == str([FAKE])
        assert wide["output"].strip() == str([FAKE, OTHER])
        assert wide["kernel"]["reused"] is False
        assert len(_KERNELS) == 2
        stubs = {k.tmpdir: open(os.path.join(k.tmpdir, "hermes_tools.py"), encoding="utf-8").read()
                 for k in _KERNELS.values()}
        assert len(set(stubs.values())) == 2


# ---- 7. Prompt-cache stability ----------------------------------------------------------------

class TestSchemaStability:
    def test_off_and_empty_are_byte_identical_to_baseline(self):
        baseline = json.dumps(build_execute_code_schema(BUILTINS, mode="strict"))
        assert json.dumps(build_execute_code_schema(BUILTINS, mode="strict",
                                                    deferred_tools=frozenset())) == baseline

    def test_on_appends_one_sentence_and_is_deterministic(self):
        names = [f"mcp__browseros__t{i:02d}" for i in range(15)]
        base = build_execute_code_schema(BUILTINS, mode="strict")
        first = build_execute_code_schema(BUILTINS, mode="strict", deferred_tools=frozenset(names))
        second = build_execute_code_schema(BUILTINS, mode="strict",
                                           deferred_tools=frozenset(reversed(names)))
        assert json.dumps(first) == json.dumps(second)
        extra = first["description"][len(base["description"]):]
        assert first["description"].startswith(base["description"]) and first["parameters"] == base["parameters"]
        assert extra.count(". ") == 0 and extra.endswith(".")
        for helper in ("call_tool", "list_deferred_tools", "describe_tool"):
            assert helper in extra
        assert ", ".join(sorted(names)[:12]) + ", +3 more." in extra
        assert sorted(names)[12] not in extra

    def test_repeated_get_tool_definitions_return_identical_bytes(self, fake_tools):
        from model_tools import _clear_tool_defs_cache, get_tool_definitions

        def execute_code_def():
            _clear_tool_defs_cache()
            defs = get_tool_definitions(enabled_toolsets=["code_execution", FAKE_TOOLSET], quiet_mode=True,
                                        skip_tool_search_assembly=True)
            return json.dumps(next(d for d in defs if d["function"]["name"] == "execute_code"))

        with _config(deferred_tools=["mcp__fake__*"]):
            first, second = execute_code_def(), execute_code_def()
        assert first == second
        assert f"{FAKE}, {OTHER}." in first
