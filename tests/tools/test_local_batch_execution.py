"""Test LOCAL_BATCH_SENTINEL execution path: concurrent local tool execution."""

import json
import time

import pytest


def test_local_batch_sentinel_executes_concurrently(monkeypatch):
    """LOCAL_BATCH_SENTINEL should execute multiple local tools concurrently via ThreadPoolExecutor."""
    import model_tools
    from tools.tool_search import LOCAL_BATCH_SENTINEL, resolve_underlying_call
    from tools.registry import registry

    # Register two local tools that we can call
    call_log = []

    def slow_tool(args, **kw):
        call_log.append(("slow", time.monotonic()))
        time.sleep(0.1)
        call_log.append(("slow_done", time.monotonic()))
        return '{"result": "slow"}'

    def fast_tool(args, **kw):
        call_log.append(("fast", time.monotonic()))
        call_log.append(("fast_done", time.monotonic()))
        return '{"result": "fast"}'

    registry.register(
        name="mcp__test__slow_tool",
        handler=slow_tool,
        schema={"name": "mcp__test__slow_tool", "description": "slow", "parameters": {}},
        toolset="mcp-test"
    )
    registry.register(
        name="mcp__test__fast_tool",
        handler=fast_tool,
        schema={"name": "mcp__test__fast_tool", "description": "fast", "parameters": {}},
        toolset="mcp-test"
    )

    try:
        # Verify resolve_underlying_call returns LOCAL_BATCH_SENTINEL for two local tools
        calls = [
            {"name": "mcp__test__slow_tool", "arguments": {}},
            {"name": "mcp__test__fast_tool", "arguments": {}},
        ]
        name, args, error = resolve_underlying_call({"calls": calls})
        assert name == LOCAL_BATCH_SENTINEL, f"Expected LOCAL_BATCH_SENTINEL, got {name}"
        assert error is None
        assert len(args["calls"]) == 2

        # Execute via handle_function_call
        result = model_tools.handle_function_call(
            "tool_call",
            {"calls": calls},
            enabled_toolsets=["mcp-test"],
            session_id="test-session",
            skip_pre_tool_call_hook=True,
            skip_tool_request_middleware=True,
            skip_tool_execution_middleware=True,
        )

        # Should succeed with both results (result is already a dict, not a JSON string)
        assert "error" not in result
        assert result["success_count"] == 2
        assert result["results"][0]["name"] == "mcp__test__slow_tool"
        assert result["results"][1]["name"] == "mcp__test__fast_tool"

        # Verify concurrent execution: fast should finish before slow even though slow started first
        # (This is a probabilistic check; with ThreadPoolExecutor both run concurrently)
        slow_start = next(t for n, t in call_log if n == "slow")
        fast_start = next(t for n, t in call_log if n == "fast")
        slow_done = next(t for n, t in call_log if n == "slow_done")
        fast_done = next(t for n, t in call_log if n == "fast_done")

        # Both should have started before either finished (concurrent)
        assert fast_done < slow_done, "Fast tool should finish before slow tool"
    finally:
        registry.deregister("mcp__test__slow_tool")
        registry.deregister("mcp__test__fast_tool")


def test_local_batch_sentinel_respects_session_scope(monkeypatch):
    """LOCAL_BATCH_SENTINEL should reject tools not in session scope."""
    import model_tools
    from tools.tool_search import LOCAL_BATCH_SENTINEL, resolve_underlying_call
    from tools.registry import registry

    # Register a tool
    registry.register(
        name="mcp__test__scoped_tool",
        handler=lambda a, **kw: '{"result": "ok"}',
        schema={"name": "mcp__test__scoped_tool", "description": "scoped", "parameters": {}},
        toolset="mcp-test"
    )

    try:
        # Try to call it without the toolset enabled
        calls = [{"name": "mcp__test__scoped_tool", "arguments": {}}]
        name, args, error = resolve_underlying_call({"calls": calls})
        # Single entry, not a batch
        assert name == "mcp__test__scoped_tool"

        # But when called via handle_function_call without the toolset, it should be rejected
        result = json.loads(model_tools.handle_function_call(
            "tool_call",
            {"calls": calls},
            enabled_toolsets=[],  # mcp-test not enabled
            session_id="test-session",
            skip_pre_tool_call_hook=True,
            skip_tool_request_middleware=True,
            skip_tool_execution_middleware=True,
        ))

        assert "error" in result
        assert "not available in this session" in result["error"]
    finally:
        registry.deregister("mcp__test__scoped_tool")


def test_local_batch_sentinel_validates_arguments(monkeypatch):
    """LOCAL_BATCH_SENTINEL should validate arguments against tool schema before executing."""
    import model_tools
    from tools.tool_search import LOCAL_BATCH_SENTINEL, resolve_underlying_call
    from tools.registry import registry

    # Register a tool with required argument
    registry.register(
        name="mcp__test__requires_arg",
        handler=lambda a, **kw: '{"result": "ok"}',
        schema={
            "name": "mcp__test__requires_arg",
            "description": "requires arg",
            "parameters": {
                "type": "object",
                "properties": {"required_field": {"type": "string"}},
                "required": ["required_field"]
            }
        },
        toolset="mcp-test"
    )

    try:
        # Call with missing required field
        calls = [{"name": "mcp__test__requires_arg", "arguments": {}}]
        name, args, error = resolve_underlying_call({"calls": calls})
        assert name == "mcp__test__requires_arg"  # Single entry

        # Execute - should fail validation before running
        result = json.loads(model_tools.handle_function_call(
            "tool_call",
            {"calls": calls},
            enabled_toolsets=["mcp-test"],
            session_id="test-session",
            skip_pre_tool_call_hook=True,
            skip_tool_request_middleware=True,
            skip_tool_execution_middleware=True,
        ))

        assert "error" in result
        assert "required" in result["error"].lower() or "missing" in result["error"].lower()
    finally:
        registry.deregister("mcp__test__requires_arg")


def test_local_batch_sentinel_preserves_input_order(monkeypatch):
    """LOCAL_BATCH_SENTINEL results should preserve the input call order."""
    import model_tools
    from tools.tool_search import LOCAL_BATCH_SENTINEL, resolve_underlying_call
    from tools.registry import registry

    call_order = []

    def tool_a(args, **kw):
        call_order.append("a_start")
        time.sleep(0.05)
        call_order.append("a_end")
        return '{"result": "A"}'

    def tool_b(args, **kw):
        call_order.append("b_start")
        call_order.append("b_end")
        return '{"result": "B"}'

    def tool_c(args, **kw):
        call_order.append("c_start")
        call_order.append("c_end")
        return '{"result": "C"}'

    registry.register(
        name="mcp__test__tool_a", handler=tool_a,
        schema={"name": "mcp__test__tool_a", "description": "a", "parameters": {}},
        toolset="mcp-test"
    )
    registry.register(
        name="mcp__test__tool_b", handler=tool_b,
        schema={"name": "mcp__test__tool_b", "description": "b", "parameters": {}},
        toolset="mcp-test"
    )
    registry.register(
        name="mcp__test__tool_c", handler=tool_c,
        schema={"name": "mcp__test__tool_c", "description": "c", "parameters": {}},
        toolset="mcp-test"
    )

    try:
        calls = [
            {"name": "mcp__test__tool_a", "arguments": {}},
            {"name": "mcp__test__tool_b", "arguments": {}},
            {"name": "mcp__test__tool_c", "arguments": {}},
        ]
        name, args, error = resolve_underlying_call({"calls": calls})
        assert name == LOCAL_BATCH_SENTINEL

        result = model_tools.handle_function_call(
            "tool_call",
            {"calls": calls},
            enabled_toolsets=["mcp-test"],
            session_id="test-session",
            skip_pre_tool_call_hook=True,
            skip_tool_request_middleware=True,
            skip_tool_execution_middleware=True,
        )

        # Results must be in same order as input (result is already a dict)
        assert result["results"][0]["name"] == "mcp__test__tool_a"
        assert result["results"][1]["name"] == "mcp__test__tool_b"
        assert result["results"][2]["name"] == "mcp__test__tool_c"
        assert result["results"][0]["result"]["result"] == "A"
        assert result["results"][1]["result"]["result"] == "B"
        assert result["results"][2]["result"]["result"] == "C"
    finally:
        registry.deregister("mcp__test__tool_a")
        registry.deregister("mcp__test__tool_b")
        registry.deregister("mcp__test__tool_c")


def test_local_batch_sentinel_handles_errors_per_entry(monkeypatch):
    """LOCAL_BATCH_SENTINEL should capture errors per entry, not fail the whole batch."""
    import model_tools
    from tools.tool_search import LOCAL_BATCH_SENTINEL, resolve_underlying_call
    from tools.registry import registry

    def failing_tool(args, **kw):
        raise ValueError("intentional failure")

    def success_tool(args, **kw):
        return '{"result": "success"}'

    registry.register(
        name="mcp__test__fail", handler=failing_tool,
        schema={"name": "mcp__test__fail", "description": "fail", "parameters": {}},
        toolset="mcp-test"
    )
    registry.register(
        name="mcp__test__success", handler=success_tool,
        schema={"name": "mcp__test__success", "description": "success", "parameters": {}},
        toolset="mcp-test"
    )

    try:
        calls = [
            {"name": "mcp__test__success", "arguments": {}},
            {"name": "mcp__test__fail", "arguments": {}},
        ]
        name, args, error = resolve_underlying_call({"calls": calls})
        assert name == LOCAL_BATCH_SENTINEL

        result = model_tools.handle_function_call(
            "tool_call",
            {"calls": calls},
            enabled_toolsets=["mcp-test"],
            session_id="test-session",
            skip_pre_tool_call_hook=True,
            skip_tool_request_middleware=True,
            skip_tool_execution_middleware=True,
        )

        # Should return both results, one with error (result is already a dict)
        assert "error" not in result  # Batch itself succeeds
        assert result["success_count"] == 1
        assert result["error_count"] == 1
        assert result["results"][0]["name"] == "mcp__test__success"
        assert result["results"][1]["name"] == "mcp__test__fail"
        assert "error" in result["results"][1]["result"]
    finally:
        registry.deregister("mcp__test__fail")
        registry.deregister("mcp__test__success")