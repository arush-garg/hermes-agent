"""Black-box checks for opt-in deferred tools in execute_code.

Set config via config.yaml in pytest's isolated Hermes home, so both schema and
execution use their real config readers. Model calls use the public dispatcher
and the real local session kernel. Only tool handlers/registrations are fake;
where observed, dispatch is wrapped, not replaced. The existing low-level RPC
entry points let us check refusal independently of the generated Python helpers.
"""

import inspect
import json
import re
import time
from functools import wraps
from textwrap import dedent

import pytest

import model_tools
from hermes_cli.config import get_config_path
from tools.code_execution_rpc import _handle_rpc_request
from tools.code_execution_tool import SANDBOX_ALLOWED_TOOLS, build_execute_code_schema
from tools.code_kernel import _KERNELS, shutdown_all_kernels
from tools.registry import registry
from tools.terminal_tool import _get_approval_callback, set_approval_callback

ECHO = "mcp__fake__echo"
WRITE = "mcp__fake__write"
UNTRUSTED = "mcp__untrusted__write"
CONNECTOR = "connectors__gmail__send"
HELPERS = ("call_tool", "list_deferred_tools", "describe_tool")
DENIED = frozenset(
    "execute_code delegate_task clarify memory skill_manage cronjob cronjob_manage "
    "send_message manage_connections tool_search tool_describe tool_call add_monitor "
    "delete_monitors todo todo_list context_notes sessions_peers".split()
)
ECHO_SCHEMA = {
    "name": ECHO,
    "description": "Echo back the supplied fixture value.",
    "parameters": {
        "type": "object",
        "properties": {"value": {}},
        "required": ["value"],
    },
}
TOOLSETS = ("code_execution", "mcp-fake")
OWNER = "code-mode-contract"


@pytest.fixture
def local_config(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    path = get_config_path()

    def configure(**options):
        config = {
            "code_execution": {"mode": "strict", "timeout": 30, **options},
            "tools": {"tool_search": {"enabled": "on", "listing": "off"}},
        }
        path.write_text(json.dumps(config), encoding="utf-8")

    configure()
    return configure


@pytest.fixture
def fake_tools():
    calls = []
    registered = []

    def register(name, toolset):
        def handle(args, **_kwargs):
            calls.append((name, dict(args)))
            if args.get("value") == "plain":
                return "fixture plain text"
            if args.get("value") == "approval":
                callback = _get_approval_callback()
                return json.dumps({"decision": callback() if callback else "missing"})
            return json.dumps(args)

        registry.register(name=name, toolset=toolset,
                          schema={**ECHO_SCHEMA, "name": name}, handler=handle)
        registered.append(name)

    try:
        register(ECHO, "mcp-fake")
        register(WRITE, "mcp-fake-write")
        register(UNTRUSTED, "mcp-untrusted")
        register(CONNECTOR, "code-contract-connectors")
        yield calls, register
    finally:
        for name in reversed(registered):
            registry.deregister(name)


@pytest.fixture
def untrusted_server(monkeypatch):
    from tools import mcp_tool
    from tools.mcp_tool_scope import _server_key

    monkeypatch.setitem(mcp_tool._server_trust_levels, _server_key("untrusted"), "untrusted")
    monkeypatch.setitem(mcp_tool._mcp_tool_server_names, UNTRUSTED, "untrusted")


@pytest.fixture
def kernel():
    shutdown_all_kernels()
    yield
    shutdown_all_kernels()


def _model_defs(enabled=TOOLSETS, disabled=(), *, quiet=True):
    return model_tools.get_tool_definitions(
        enabled_toolsets=list(enabled), disabled_toolsets=list(disabled), quiet_mode=quiet,
    )


def _execute_schema(definitions):
    return next(td["function"] for td in definitions if td["function"]["name"] == "execute_code")


def _bytes(value):
    # Preserve key order to catch changes that a dict equality check would miss.
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _cell(code, *, enabled=TOOLSETS, disabled=()):
    tool_names = [td["function"]["name"] for td in _model_defs(enabled, disabled)]
    result = json.loads(model_tools.handle_function_call(
        "execute_code", {"code": dedent(code)}, task_id=OWNER, enabled_tools=tool_names,
        enabled_toolsets=list(enabled), disabled_toolsets=list(disabled),
    ))
    assert result.get("status") == "success", result
    assert result["kernel"]["mode"] == "session", result
    return result, json.loads(result["output"])


def _assert_refused(result, name):
    assert result["error"].startswith(f"Tool '{name}' is not available in execute_code."), result


def test_off_by_default_has_no_helpers_or_rpc_authority(local_config, fake_tools, kernel):
    calls, _ = fake_tools
    assert _bytes(_execute_schema(_model_defs())) == _bytes(build_execute_code_schema(set(), mode="strict"))
    result, observed = _cell(f"""
        import json
        import hermes_tools
        missing = [name for name in {HELPERS!r} if not hasattr(hermes_tools, name)]
        try:
            from hermes_tools import call_tool
        except ImportError:
            import_failed = True
        else:
            import_failed = False
        print(json.dumps([missing, import_failed, hermes_tools._call({ECHO!r}, {{'value': 1}})]))
    """)
    assert observed[:2] == [list(HELPERS), True]
    _assert_refused(observed[2], ECHO)
    assert result["tool_calls_made"] == 0
    assert calls == []


def test_raw_request_cannot_dispatch_a_name_outside_the_cell_grant(fake_tools):
    calls, _ = fake_tools
    counter, log = [0], []
    # Effective built-in grant (empty) union the resolved deferred grant (echo only).
    allowed = frozenset({ECHO})
    reply = _handle_rpc_request(
        {"tool": WRITE, "args": {"value": 1}}, allowed_tools=allowed,
        tool_call_counter=counter, max_tool_calls=1,
        dispatch=model_tools.handle_function_call, tool_call_log=log,
        call_start=time.monotonic(), where="contract test",
    )
    _assert_refused(json.loads(reply), WRITE)
    assert counter == [0] and log == [] and calls == []


def test_resolver_globs_and_hard_exclusions(fake_tools, untrusted_server):
    from tools.code_execution_deferred import DENIED_PREFIXES, DENIED_TOOLS, resolve_deferred_tools

    assert isinstance(DENIED_TOOLS, frozenset)
    assert DENIED <= DENIED_TOOLS
    assert isinstance(DENIED_PREFIXES, tuple)
    assert "connectors__" in DENIED_PREFIXES
    names = (ECHO, WRITE, "process_manage", UNTRUSTED, CONNECTOR, *DENIED, *SANDBOX_ALLOWED_TOOLS)
    resolved = resolve_deferred_tools(names, ("mcp__fake__e?ho", "process_manage"), backend_is_local=True)
    assert resolved == frozenset({ECHO, "process_manage"})
    assert resolve_deferred_tools(names, ("MCP__FAKE__*",), backend_is_local=True) == frozenset()
    resolved = resolve_deferred_tools(names, ("*",), backend_is_local=True)
    assert isinstance(resolved, frozenset)
    assert resolved == frozenset({ECHO, WRITE, "process_manage"})
    assert resolve_deferred_tools(tuple(reversed(names)), ("*",), backend_is_local=True) == resolved
    assert resolve_deferred_tools(names, (), backend_is_local=True) == frozenset()
    assert resolve_deferred_tools(names, ("*",), backend_is_local=False) == frozenset()


@pytest.mark.parametrize("bad_patterns", ["*", ["*", 42], {"pattern": "*"}], ids=["scalar", "mixed-list", "mapping"])
def test_bad_config_fails_closed_and_warns(local_config, caplog, bad_patterns):
    from tools.code_execution_deferred import load_deferred_config

    local_config(deferred_tools=bad_patterns)
    assert load_deferred_config() == ((), 25)
    assert any(record.levelname == "WARNING" and "deferred_tools" in record.getMessage()
               for record in caplog.records)


@pytest.mark.parametrize(("configured", "expected"), [(0, 1), (17, 17), (201, 200)])
def test_config_preserves_patterns_and_clamps_integer_budget(local_config, configured, expected):
    from tools.code_execution_deferred import load_deferred_config

    patterns = ("mcp__fake__*", "process_manage")
    local_config(deferred_tools=list(patterns), max_deferred_tool_calls=configured)
    assert load_deferred_config() == (patterns, expected)


@pytest.mark.parametrize("explicitly_disabled", [False, True], ids=["not-enabled", "disabled"])
def test_session_scope_applies_to_helpers_and_rpc(local_config, fake_tools, kernel, explicitly_disabled):
    local_config(deferred_tools=["mcp__fake__*"], max_deferred_tool_calls=1)
    calls, _ = fake_tools
    enabled = (*TOOLSETS, "mcp-fake-write") if explicitly_disabled else TOOLSETS
    disabled = ("mcp-fake-write",) if explicitly_disabled else ()
    visible = {td["function"]["name"] for td in _model_defs(enabled, disabled)}
    result, observed = _cell(f"""
        import json
        from hermes_tools import call_tool, list_deferred_tools, describe_tool, _call
        print(json.dumps([
            list_deferred_tools(), describe_tool({WRITE!r}),
            call_tool({WRITE!r}, value=1), _call({WRITE!r}, {{'value': 2}}),
            call_tool({ECHO!r}, value=3),
        ]))
    """, enabled=enabled, disabled=disabled)
    assert "tool_search" in visible and ECHO not in visible
    assert observed[0] == [ECHO]
    assert isinstance(observed[1], dict) and observed[1].get("error")
    for blocked in observed[2:4]:
        _assert_refused(blocked, WRITE)
    assert observed[4] == {"value": 3}
    assert result["tool_calls_made"] == 1
    assert calls == [(ECHO, {"value": 3})]


def test_kernel_helpers_budget_and_real_name_dispatch(local_config, fake_tools, kernel, monkeypatch):
    local_config(deferred_tools=["*"], max_deferred_tool_calls=25)
    calls, _ = fake_tools
    routed = []
    dispatch = model_tools.handle_function_call
    signature = inspect.signature(dispatch)

    @wraps(dispatch)
    def observe(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        if bound.arguments["function_name"] == ECHO:
            routed.append((bound.arguments.get("enabled_toolsets"), bound.arguments.get("disabled_toolsets")))
        return dispatch(*args, **kwargs)

    monkeypatch.setattr(model_tools, "handle_function_call", observe)
    result, (names, schema, refusals, replies) = _cell(f"""
        import json
        from hermes_tools import call_tool, list_deferred_tools, describe_tool
        refusals = [call_tool(name) for name in ('execute_code', 'tool_call', 'delegate_task')]
        replies = [call_tool({ECHO!r}, value='plain' if i == 0 else i) for i in range(30)]
        print(json.dumps([list_deferred_tools(), describe_tool({ECHO!r}), refusals, replies]))
    """, enabled=(*TOOLSETS, "mcp-fake-write"), disabled=("mcp-fake-write",))
    assert names == [ECHO] and schema == ECHO_SCHEMA
    for name, refusal in zip(("execute_code", "tool_call", "delegate_task"), refusals, strict=True):
        _assert_refused(refusal, name)
    values = ["plain", *range(1, 25)]
    assert len(replies) == 30
    assert replies[:25] == ["fixture plain text", *({"value": i} for i in range(1, 25))]
    for reply in replies[25:]:
        assert reply["error"].startswith(f"Deferred tool call limit reached (25). Call to '{ECHO}' blocked")
    assert result["tool_calls_made"] == 25
    assert calls == [(ECHO, {"value": value}) for value in values]
    assert len(routed) == 25
    for enabled, disabled in routed:
        assert set(enabled or ()) == {*TOOLSETS, "mcp-fake-write"}
        assert set(disabled or ()) == {"mcp-fake-write"}
    (active_kernel,) = _KERNELS.values()
    deferred_entries = [entry for entry in active_kernel.tool_call_log if entry["tool"] == ECHO]
    assert [entry.get("deferred") for entry in deferred_entries] == [True] * 25


def test_existing_total_budget_includes_deferred_calls(local_config, fake_tools, kernel, tmp_path):
    local_config(deferred_tools=[ECHO], max_deferred_tool_calls=25, max_tool_calls=2)
    calls, _ = fake_tools
    path = tmp_path / "fixture.txt"
    path.write_text("built-in call succeeded", encoding="utf-8")
    result, (first, builtin, last) = _cell(f"""
        import json
        from hermes_tools import call_tool, read_file
        print(json.dumps([call_tool({ECHO!r}, value=1), read_file({str(path)!r}),
                          call_tool({ECHO!r}, value=2)]))
    """, enabled=(*TOOLSETS, "file"))
    assert first == {"value": 1}
    assert "built-in call succeeded" in builtin["content"]
    assert last["error"].startswith("Tool call limit reached (2).")
    assert result["tool_calls_made"] == 2
    assert calls == [(ECHO, {"value": 1})]


def test_reused_kernel_uses_each_cells_approval_callback_and_budget(local_config, fake_tools, kernel):
    local_config(deferred_tools=[ECHO], max_deferred_tool_calls=1)
    code = f"""
        import json
        from hermes_tools import call_tool
        print(json.dumps(call_tool({ECHO!r}, value='approval')))
    """
    prior = _get_approval_callback()
    try:
        set_approval_callback(lambda: "first")
        first, first_reply = _cell(code)
        set_approval_callback(lambda: "second")
        second, second_reply = _cell(code)
    finally:
        set_approval_callback(prior)
    assert first_reply == {"decision": "first"}
    assert second_reply == {"decision": "second"}
    assert first["tool_calls_made"] == second["tool_calls_made"] == 1
    assert second["kernel"]["reused"] is True


def test_kernel_identity_includes_effective_deferred_set(local_config, fake_tools, kernel):
    enabled = (*TOOLSETS, "mcp-fake-write")
    local_config(deferred_tools=[ECHO])
    _, first = _cell("""
        import json
        from hermes_tools import list_deferred_tools
        marker = 123
        print(json.dumps(list_deferred_tools()))
    """, enabled=enabled)
    local_config(deferred_tools=[WRITE])
    changed, (names, old_state) = _cell("""
        import json
        from hermes_tools import list_deferred_tools
        print(json.dumps([list_deferred_tools(), globals().get('marker')]))
    """, enabled=enabled)
    assert first == [ECHO]
    assert names == [WRITE] and old_state is None
    assert changed["kernel"]["reused"] is False


def test_schema_opt_in_is_one_stable_bounded_append(local_config, fake_tools):
    _, register = fake_tools
    extra = [f"mcp__fake__contract_{i:02d}" for i in range(13)]
    for name in reversed(extra):
        register(name, "mcp-fake")
    expected = sorted([ECHO, *extra])

    baseline = build_execute_code_schema(set(), mode="strict")
    assert _bytes(_execute_schema(_model_defs())) == _bytes(baseline)
    local_config(deferred_tools=["mcp__no_such_server__*"])
    assert _bytes(build_execute_code_schema(set(), mode="strict")) == _bytes(baseline)
    assert _bytes(_execute_schema(_model_defs())) == _bytes(baseline)

    local_config(deferred_tools=["mcp__fake__*"])
    first = _model_defs()
    assert _bytes(first) == _bytes(_model_defs())
    assert _bytes(first) == _bytes(_model_defs(quiet=False))
    schema = _execute_schema(first)
    prefix = baseline["description"]
    assert schema["description"].startswith(prefix)
    assert _bytes({**schema, "description": prefix}) == _bytes(baseline)
    appended = schema["description"][len(prefix):].strip()
    assert all(re.search(rf"\b{helper}\b", appended) for helper in HELPERS), appended
    assert re.findall(r"\bmcp__\w+", appended) == expected[:12], appended
    assert "+2 more" in appended
    # Delimiter punctuation (including an ellipsis) is not another sentence.
    assert not re.search(r"(?<![.!?])[.!?]\s+\w", appended), appended
