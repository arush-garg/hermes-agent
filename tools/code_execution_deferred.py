"""Opted-in deferred and MCP tools inside execute_code ("code mode").

``code_execution.deferred_tools`` lists fnmatch globs over tool names. Matching tools this
session is granted (model-visible or deferred behind tool_search) become callable from a
local session kernel through ``hermes_tools.call_tool``. The host RPC handler is the
security boundary: it holds the resolved set and a separate per-cell budget, and every
call goes through ``model_tools.handle_function_call`` with the session's toolset scope so
hooks, middleware, MCP trust approval and deferred routing all run. Off (no patterns) adds
nothing: no helpers, no schema bytes, no RPC surface.

Design adapted from upstream PRs #82243 (deferred tools in execute_code) and #98615
(read-only MCP with a separate budget and a kernel keyed by tool set).
"""

import fnmatch
import json
import logging
from typing import Any, Callable, Iterable, Optional

from tools.registry import tool_error

logger = logging.getLogger(__name__)

# Never callable from code, whatever the patterns say: agent-loop and agent spawning,
# user-interaction, persistence/scheduling/installs, outward messaging, credentials and the
# tool_search bridge itself. ``is_denied`` also refuses model_tools._AGENT_LOOP_TOOLS.
DENIED_TOOLS = frozenset({
    "execute_code", "delegate_task", "clarify", "memory", "skill_manage", "cronjob",
    "cronjob_manage", "send_message", "manage_connections", "tool_search", "tool_describe",
    "tool_call", "add_monitor", "delete_monitors", "todo", "todo_list", "context_notes",
    "sessions_peers", "react_to_message", "a2a_call", "a2a_orchestrate", "manage_catalog",
    "browser_vault", "get_delegate_result", "feishu_drive_reply_comment", "feishu_drive_add_comment",
})
# Hosted connector accounts and Discord are outward-facing; kanban_* schedules and spawns
# agents; a2a_* talks to other agents.
DENIED_PREFIXES: tuple[str, ...] = ("connectors__", "discord", "kanban_", "a2a_")

DEFAULT_MAX_DEFERRED_CALLS = 25
_MAX_DEFERRED_CALLS_RANGE = (1, 200)
_SCHEMA_NAME_LIMIT = 12
# Host-side pseudo tools behind describe_tool() / list_deferred_tools(): answered from the
# CURRENT cell's scope, never dispatched, free (no budget, not logged).
DESCRIBE_RPC_NAME = "__describe_tool__"
LIST_RPC_NAME = "__list_deferred_tools__"
PSEUDO_RPC_NAMES = frozenset({DESCRIBE_RPC_NAME, LIST_RPC_NAME})
_MCP_PREFIX = "mcp__"


def _is_untrusted_mcp_tool(name: str) -> bool:
    """True when *name* is an MCP tool whose server is configured ``trust: untrusted``.
    Uses the provenance recorded at registration (raw server name) and the consuming
    profile's trust table; a tool with no recorded provenance falls back to matching the
    sanitized server segment of ``mcp__<server>__<tool>``."""
    if not name.startswith(_MCP_PREFIX):
        return False
    try:
        from tools import mcp_tool as core
        from tools.mcp_tool_schema import sanitize_mcp_name_component
        from tools.mcp_tool_scope import _key_name, _server_key
    except ImportError:
        return False
    server = core._mcp_tool_server_names.get(name)
    if server is not None:
        return core._server_trust_levels.get(_server_key(server), core._TRUST_FULL) == core._TRUST_UNTRUSTED
    segment = name[len(_MCP_PREFIX):].split("__", 1)[0]
    return any(level == core._TRUST_UNTRUSTED and sanitize_mcp_name_component(_key_name(key)) == segment
               for key, level in list(core._server_trust_levels.items()))


def is_denied(name: str) -> bool:
    """True for a name code may never call: DENIED_TOOLS, DENIED_PREFIXES, or a tool only the
    agent loop can run (``model_tools._AGENT_LOOP_TOOLS``, e.g. session_search)."""
    from model_tools import _AGENT_LOOP_TOOLS
    return name in DENIED_TOOLS or name in _AGENT_LOOP_TOOLS or name.startswith(DENIED_PREFIXES)


def resolve_deferred_tools(session_tool_names: Iterable[str], patterns: Iterable[str], *,
                           backend_is_local: bool,
                           is_untrusted: Optional[Callable[[str], bool]] = None) -> frozenset[str]:
    """Session tools matching any pattern, minus the denylist, the direct sandbox helpers
    and tools on untrusted MCP servers. Empty on a remote backend or with no patterns."""
    patterns = tuple(patterns or ())
    if not backend_is_local or not patterns:
        return frozenset()
    from tools.code_execution_tool import SANDBOX_ALLOWED_TOOLS
    untrusted = is_untrusted or _is_untrusted_mcp_tool
    return frozenset(
        name for name in set(session_tool_names or ())
        if isinstance(name, str) and any(fnmatch.fnmatchcase(name, p) for p in patterns)
        and not is_denied(name) and name not in SANDBOX_ALLOWED_TOOLS and not untrusted(name)
    )


def _clamped_calls(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        if value is not None:
            logger.warning("Ignoring code_execution.max_deferred_tool_calls=%r (expected an integer); using %d",
                           value, DEFAULT_MAX_DEFERRED_CALLS)
        return DEFAULT_MAX_DEFERRED_CALLS
    low, high = _MAX_DEFERRED_CALLS_RANGE
    return min(high, max(low, value))


def _patterns_from(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or not all(isinstance(p, str) for p in value):
        logger.warning("Ignoring code_execution.deferred_tools=%r (expected a list of glob strings); "
                       "deferred tools stay off", value)
        return ()
    return tuple(p for p in value if p)


def load_deferred_config() -> tuple[tuple[str, ...], int]:
    """``(patterns, max_deferred_calls)`` from ``code_execution``; garbage fails closed."""
    from tools import code_execution_tool
    cfg = code_execution_tool._load_config()
    return _patterns_from(cfg.get("deferred_tools")), _clamped_calls(cfg.get("max_deferred_tool_calls"))


def backend_is_local() -> bool:
    """True only for the local terminal backend (fail closed on an unresolvable config)."""
    try:
        from tools.terminal_tool import _get_env_config
        return _get_env_config().get("env_type") == "local"
    except Exception:
        logger.debug("Could not resolve the terminal backend for deferred tools", exc_info=True)
        return False


def session_tool_defs(enabled_toolsets: Optional[list[str]],
                      disabled_toolsets: Optional[list[str]]) -> list[dict]:
    """The session's granted tool catalog, uncollapsed by tool_search: the same
    ``get_tool_definitions`` call the agent builds its tool list from and the tool_search
    bridge scopes ``tool_call`` with, so a deferred MCP tool counts while a tool outside the
    session's enabled/disabled toolsets (or failing its check_fn) does not."""
    from model_tools import get_tool_definitions
    return get_tool_definitions(enabled_toolsets=enabled_toolsets, disabled_toolsets=disabled_toolsets,
                                quiet_mode=True, skip_tool_search_assembly=True) or []


class DeferredScope:
    """One cell's deferred grant: the allowed names, a per-cell budget, the session schemas
    describe_tool reads, and the toolset scope every deferred dispatch carries."""

    def __init__(self, names: frozenset, max_calls: int, schemas: dict[str, dict],
                 enabled_toolsets: Optional[list[str]] = None,
                 disabled_toolsets: Optional[list[str]] = None):
        self.names = frozenset(names)
        self.max_calls = max_calls
        self.calls = 0
        self.schemas = schemas
        self.dispatch_kwargs = {"enabled_toolsets": enabled_toolsets, "disabled_toolsets": disabled_toolsets}

    def limit_error(self, tool_name: str) -> Optional[str]:
        """The refusal for a deferred call past the budget, else None."""
        if self.calls < self.max_calls:
            return None
        return tool_error(f"Deferred tool call limit reached ({self.max_calls}). Call to '{tool_name}' "
                          "blocked; no more deferred tool calls are allowed in this execution.")

    def pseudo_call(self, tool_name: str, args: Any) -> str:
        """Answer one host pseudo-call (``PSEUDO_RPC_NAMES``) from this scope."""
        if tool_name == LIST_RPC_NAME:
            return json.dumps(sorted(self.names))
        return self.describe(args)

    def describe(self, args: Any) -> str:
        name = args.get("name") if isinstance(args, dict) else None
        if not isinstance(name, str) or name not in self.names:
            return tool_error(f"Tool '{name}' is not a deferred tool available in execute_code. "
                              f"Available: {', '.join(sorted(self.names))}")
        fn = self.schemas.get(name, {})
        return json.dumps({"name": name, "description": fn.get("description", ""),
                           "parameters": fn.get("parameters", {})}, ensure_ascii=False)


def build_deferred_scope(enabled_toolsets: Optional[list[str]],
                         disabled_toolsets: Optional[list[str]]) -> Optional[DeferredScope]:
    """The cell's DeferredScope, or None when the feature is off or nothing resolves."""
    patterns, max_calls = load_deferred_config()
    if not patterns or not backend_is_local():
        return None
    defs = session_tool_defs(enabled_toolsets, disabled_toolsets)
    fns = {td["function"]["name"]: td["function"] for td in defs if "function" in td}
    names = resolve_deferred_tools(fns, patterns, backend_is_local=True)
    if not names:
        return None
    return DeferredScope(names, max_calls, {n: fns[n] for n in names}, enabled_toolsets, disabled_toolsets)


def deferred_schema_sentence(names: Iterable[str]) -> str:
    """The one sentence the execute_code description gains when deferred tools resolve."""
    ordered = sorted(names)
    listed = ", ".join(ordered[:_SCHEMA_NAME_LIMIT])
    more = f", +{len(ordered) - _SCHEMA_NAME_LIMIT} more" if len(ordered) > _SCHEMA_NAME_LIMIT else ""
    return ("Opted-in deferred tools (batch 3+ already-decided calls to them, such as a loop over "
            "pages, tabs or items, in ONE execute_code cell instead of one tool_call per step) are "
            "callable with `from hermes_tools import call_tool, list_deferred_tools, describe_tool` "
            "as call_tool(name, **kwargs) (describe_tool(name) returns parameters; separate per-cell "
            f"budget): {listed}{more}.")


def with_deferred_execute_code(tool_defs: list[dict]) -> list[dict]:
    """Rebuild the execute_code definition with the deferred sentence when the session's
    granted tools resolve a non-empty set; otherwise return *tool_defs* untouched."""
    patterns, _ = load_deferred_config()
    names = {td["function"]["name"] for td in tool_defs}
    if not patterns or "execute_code" not in names:
        return tool_defs
    resolved = resolve_deferred_tools(names, patterns, backend_is_local=backend_is_local())
    if not resolved:
        return tool_defs
    from tools.code_execution_tool import SANDBOX_ALLOWED_TOOLS, _get_execution_mode, build_execute_code_schema
    schema = build_execute_code_schema(SANDBOX_ALLOWED_TOOLS & names, mode=_get_execution_mode(),
                                       deferred_tools=resolved)
    return [{"type": "function", "function": schema} if td["function"]["name"] == "execute_code" else td
            for td in tool_defs]


_HELPERS_TEMPLATE = '''
# ---------------------------------------------------------------------------
# Opted-in deferred tools (code_execution.deferred_tools)
# ---------------------------------------------------------------------------

def call_tool(name, /, **kwargs):
    """Call one opted-in deferred tool with kwargs as its arguments. Returns the parsed
    JSON result (dict), or the raw string when the result is not JSON."""
    return _call(name, kwargs)


def list_deferred_tools():
    """Sorted names callable through call_tool() in this cell."""
    return _call({list_name!r}, {{}})


def describe_tool(name):
    """{{"name", "description", "parameters"}} for one deferred tool."""
    return _call({describe!r}, {{"name": name}})
'''


def deferred_helpers_source(enabled: bool) -> str:
    """hermes_tools source for call_tool / list_deferred_tools / describe_tool ('' when off).
    It embeds no names: both list and describe ask the host for the CURRENT cell's scope, so
    one kernel serves a tool set that changes between cells."""
    return _HELPERS_TEMPLATE.format(list_name=LIST_RPC_NAME, describe=DESCRIBE_RPC_NAME) if enabled else ""
