"""Live, in-process MCP bridge that exposes Hermes' FULL tool surface to a
spawned `claude` CLI subprocess.

Why this exists
---------------
The `claude-cli` provider runs Claude turns through the locally-installed
`claude` binary so they bill against the Claude Pro/Max plan (first-party),
not "extra usage". Claude Code owns the inner loop for that turn — so by
default it would only have its own built-in tools (Bash/Read/Edit/...).

To make ALL of Hermes' tools reachable inside a Claude turn — web search,
browser automation, vision, image generation, skills, TTS, kanban, AND the
agent-loop tools (todo / memory / session_search / delegate_task) — we run an
MCP server *inside the live Hermes process*, bound to the running ``AIAgent``,
and point the subprocess at it via ``--mcp-config`` (HTTP transport).

Unlike :mod:`agent.transports.hermes_tools_mcp_server` (a stateless subprocess
used by the codex runtime, which deliberately cannot reach the agent-loop
tools), this server holds a reference to the live agent, so the four
``_AGENT_LOOP_TOOLS`` dispatch against real agent state — matching what the
normal Hermes loop does in ``agent/tool_executor.py``.

Transport: low-level ``mcp.server.lowlevel.Server`` (full control over each
tool's ``inputSchema``) served over Streamable HTTP on a localhost port, run
by uvicorn on a daemon thread for the lifetime of the owning client.
"""

from __future__ import annotations

import contextlib
import json
import logging
import socket
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# The four tools the normal Hermes loop intercepts because they need live
# agent state (TodoStore / MemoryStore / session DB / subagent dispatch).
# See model_tools._AGENT_LOOP_TOOLS and agent/tool_executor.py.
_AGENT_LOOP_TOOLS = {"todo", "memory", "session_search", "delegate_task"}


def _free_port(host: str) -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


class LiveToolsMCPServer:
    """In-process Streamable-HTTP MCP server exposing all Hermes tools.

    Bound to a live ``AIAgent`` so agent-loop tools work. Start it with
    :meth:`start` (idempotent), read :attr:`url`, and tear it down with
    :meth:`stop`.
    """

    def __init__(
        self,
        agent: Any = None,
        *,
        host: str = "127.0.0.1",
        port: Optional[int] = None,
        enabled_toolsets: Optional[list[str]] = None,
        disabled_toolsets: Optional[list[str]] = None,
        task_id: Optional[str] = None,
    ) -> None:
        self._agent = agent
        self._host = host
        self._port = port or _free_port(host)
        self._enabled_toolsets = enabled_toolsets
        self._disabled_toolsets = disabled_toolsets
        self._task_id = task_id
        self.url = f"http://{host}:{self._port}/mcp"

        self._uvicorn = None  # uvicorn.Server
        self._thread: Optional[threading.Thread] = None
        self._tool_defs: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._started = False

        # Hooked by ClaudeCLIClient so tool invocations show in the terminal.
        # Signature: on_tool_call(name: str, args: dict) -> None
        self.on_tool_call: Optional[Any] = None

    # ── tool catalog ────────────────────────────────────────────────
    def _load_tool_defs(self) -> dict[str, dict]:
        """Return {name: function-spec} for the FULL Hermes tool surface.

        ``skip_tool_search_assembly=True`` keeps the real catalog from being
        collapsed into the tool_search bridge, so Claude sees every tool
        directly rather than a deferred stub.
        """
        from model_tools import get_tool_definitions

        defs: dict[str, dict] = {}
        try:
            raw = get_tool_definitions(
                enabled_toolsets=self._enabled_toolsets,
                disabled_toolsets=self._disabled_toolsets,
                quiet_mode=True,
                skip_tool_search_assembly=True,
            ) or []
        except TypeError:
            # Older signature without skip_tool_search_assembly.
            raw = get_tool_definitions(quiet_mode=True) or []
        for td in raw:
            if not isinstance(td, dict) or td.get("type") != "function":
                continue
            fn = td.get("function") or {}
            name = fn.get("name")
            if isinstance(name, str) and name.strip():
                defs[name.strip()] = fn
        return defs

    # ── dispatch ────────────────────────────────────────────────────
    def _dispatch_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Execute one Hermes tool and return its result as a string.

        Agent-loop tools dispatch against the live agent (mirroring
        ``agent/tool_executor.py``); everything else goes through the
        stateless ``model_tools.handle_function_call`` dispatcher.
        """
        args = arguments if isinstance(arguments, dict) else {}
        # Notify the UI that a tool is being invoked so users see progress.
        if self.on_tool_call is not None:
            try:
                self.on_tool_call(name, args)
            except Exception:
                pass
        try:
            if name in _AGENT_LOOP_TOOLS and self._agent is not None:
                return self._dispatch_agent_loop_tool(name, args)
            from model_tools import handle_function_call

            return handle_function_call(
                name,
                args,
                task_id=self._task_id,
                session_id=getattr(self._agent, "session_id", None),
                enabled_toolsets=self._enabled_toolsets,
                disabled_toolsets=self._disabled_toolsets,
            )
        except Exception as exc:  # never let a tool error kill the bridge
            logger.exception("hermes live tool %s raised", name)
            return json.dumps({"error": str(exc), "tool": name}, ensure_ascii=False)

    def _dispatch_agent_loop_tool(self, name: str, args: dict[str, Any]) -> str:
        """Dispatch todo/memory/session_search/delegate_task against the live
        agent, mirroring the branches in ``agent/tool_executor.py``."""
        agent = self._agent
        if name == "todo":
            from tools.todo_tool import todo_tool
            return todo_tool(
                todos=args.get("todos"),
                merge=args.get("merge", False),
                store=agent._todo_store,
            )
        if name == "memory":
            from tools.memory_tool import memory_tool
            target = args.get("target", "memory")
            result = memory_tool(
                action=args.get("action"),
                target=target,
                content=args.get("content"),
                old_text=args.get("old_text"),
                store=agent._memory_store,
            )
            mgr = getattr(agent, "_memory_manager", None)
            if mgr and args.get("action") in {"add", "replace"}:
                try:
                    mgr.on_memory_write(
                        args.get("action", ""), target, args.get("content", ""),
                    )
                except Exception:
                    pass
            return result
        if name == "session_search":
            session_db = agent._get_session_db_for_recall()
            if not session_db:
                from hermes_state import format_session_db_unavailable
                return json.dumps(
                    {"success": False, "error": format_session_db_unavailable()}
                )
            from tools.session_search_tool import session_search
            return session_search(
                query=args.get("query", ""),
                role_filter=args.get("role_filter"),
                limit=args.get("limit", 3),
                session_id=args.get("session_id"),
                around_message_id=args.get("around_message_id"),
                window=args.get("window", 5),
                sort=args.get("sort"),
                db=session_db,
                current_session_id=agent.session_id,
            )
        if name == "delegate_task":
            return agent._dispatch_delegate_task(args)
        return json.dumps({"error": f"unhandled agent-loop tool {name}"})

    # ── ASGI app / server ───────────────────────────────────────────
    def _build_app(self):
        import mcp.types as types
        from mcp.server.lowlevel import Server
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        from starlette.applications import Starlette
        from starlette.routing import Mount

        self._tool_defs = self._load_tool_defs()
        server: Any = Server("hermes-live-tools")

        @server.list_tools()
        async def _list_tools() -> list[Any]:
            tools = []
            for name, fn in self._tool_defs.items():
                schema = fn.get("parameters") or {"type": "object", "properties": {}}
                tools.append(
                    types.Tool(
                        name=name,
                        description=(fn.get("description") or f"Hermes {name} tool")[:1024],
                        inputSchema=schema,
                    )
                )
            return tools

        @server.call_tool()
        async def _call_tool(name: str, arguments: dict[str, Any]) -> list[Any]:
            import anyio

            result = await anyio.to_thread.run_sync(
                lambda: self._dispatch_tool(name, arguments)
            )
            if not isinstance(result, str):
                result = json.dumps(result, ensure_ascii=False, default=str)
            return [types.TextContent(type="text", text=result)]

        session_manager = StreamableHTTPSessionManager(
            app=server,
            json_response=True,
            stateless=True,
        )

        async def _handle(scope, receive, send):
            await session_manager.handle_request(scope, receive, send)

        @contextlib.asynccontextmanager
        async def _lifespan(_app):
            async with session_manager.run():
                yield

        return Starlette(routes=[Mount("/mcp", app=_handle)], lifespan=_lifespan)

    def start(self) -> "LiveToolsMCPServer":
        with self._lock:
            if self._started:
                return self
            import uvicorn

            app = self._build_app()
            config = uvicorn.Config(
                app,
                host=self._host,
                port=self._port,
                log_level="warning",
                lifespan="on",
                access_log=False,
            )
            self._uvicorn = uvicorn.Server(config)
            self._thread = threading.Thread(
                target=self._uvicorn.run,
                name=f"hermes-live-mcp:{self._port}",
                daemon=True,
            )
            self._thread.start()

            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if getattr(self._uvicorn, "started", False):
                    break
                if not self._thread.is_alive():
                    raise RuntimeError("Hermes live MCP bridge thread died on startup")
                time.sleep(0.02)
            else:
                raise TimeoutError("Hermes live MCP bridge did not start within 10s")

            self._started = True
            logger.info(
                "Hermes live MCP bridge listening at %s (%d tools)",
                self.url,
                len(self._tool_defs),
            )
            return self

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            self._started = False
            srv = self._uvicorn
        if srv is not None:
            srv.should_exit = True
        thread = self._thread
        if thread is not None:
            thread.join(timeout=3.0)

    @property
    def tool_count(self) -> int:
        return len(self._tool_defs)
