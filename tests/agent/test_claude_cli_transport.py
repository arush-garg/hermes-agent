"""Tests for the claude-cli (Claude Code subscription) provider.

Covers the ClaudeCLIClient subprocess shim, the in-process live MCP tools
bridge dispatch routing, env scrubbing, provider registration, the
create_openai_client factory branch, and runtime-provider resolution.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


# ── prompt formatting ────────────────────────────────────────────────
def test_split_system_and_transcript():
    from agent.claude_cli_client import _split_system_and_transcript

    messages = [
        {"role": "system", "content": "You are Hermes."},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello", "tool_calls": [
            {"function": {"name": "web_search"}},
        ]},
        {"role": "tool", "content": "result text"},
    ]
    system, transcript = _split_system_and_transcript(messages)
    assert system == "You are Hermes."
    assert "User:\nhi" in transcript
    assert "Assistant:\nhello" in transcript
    assert "[called tools: web_search]" in transcript
    assert "Tool result:\nresult text" in transcript


# ── env scrubbing (the auth footgun) ─────────────────────────────────
def test_build_env_scrubs_conflicting_auth(monkeypatch):
    from agent.claude_cli_client import ClaudeCLIClient

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-should-not-leak")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.example/v1")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "leak")
    monkeypatch.setenv("HOME", "/home/tester")

    client = ClaudeCLIClient(agent=None)
    env = client._build_env("sk-ant-oat-token")

    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-token"
    assert env["CLAUDECODE"] == ""  # never inherit a parent Claude Code session
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_BASE_URL" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["HOME"] == "/home/tester"


# ── stream/json result parsing ───────────────────────────────────────
def _make_stream_proc(events: list) -> object:
    """Return a fake Popen whose stdout iterates NDJSON lines."""
    import io
    lines = [json.dumps(e) + "\n" for e in events]
    class _FakeProc:
        returncode = 0
        stdout = io.StringIO("".join(lines))
        stderr = io.StringIO("")
        def wait(self): pass
    return _FakeProc()


def test_run_parses_claude_json(monkeypatch):
    from agent.claude_cli_client import ClaudeCLIClient

    events = [
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "the answer is 42"}]}},
        {"type": "result", "subtype": "success", "is_error": False,
         "usage": {"input_tokens": 11, "output_tokens": 7, "cache_read_input_tokens": 3}},
    ]
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: _make_stream_proc(events))
    client = ClaudeCLIClient(agent=None)
    text, usage = client._run(["claude", "-p", "x"], "tok", 30.0)
    assert text == "the answer is 42"
    assert usage["input_tokens"] == 11 and usage["output_tokens"] == 7


def test_run_raises_on_api_error(monkeypatch):
    from agent.claude_cli_client import ClaudeCLIClient

    events = [
        {"type": "result", "subtype": "error_during_execution", "is_error": True,
         "api_error_status": 401, "result": "Failed to authenticate"},
    ]
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: _make_stream_proc(events))
    client = ClaudeCLIClient(agent=None)
    with pytest.raises(RuntimeError) as exc:
        client._run(["claude", "-p", "x"], "tok", 30.0, max_attempts=1)
    assert "401" in str(exc.value)


def test_create_chat_completion_shape(monkeypatch):
    """End-to-end shape test with the subprocess + bridge mocked out."""
    from agent.claude_cli_client import ClaudeCLIClient

    client = ClaudeCLIClient(agent=None)
    monkeypatch.setattr(client, "_resolve_token", lambda: "sk-ant-oat-x")
    monkeypatch.setattr(client, "_ensure_bridge", lambda: SimpleNamespace(url="http://127.0.0.1:9/mcp"))
    monkeypatch.setattr(client, "_run", lambda cmd, tok, t, **kw: ("done", {"input_tokens": 5, "output_tokens": 2}))

    resp = client.chat.completions.create(
        model="claude-cli/haiku",
        messages=[{"role": "user", "content": "go"}],
    )
    msg = resp.choices[0].message
    assert msg.content == "done"
    assert msg.tool_calls == []
    assert resp.choices[0].finish_reason == "stop"
    assert resp.usage.prompt_tokens == 5 and resp.usage.completion_tokens == 2


# ── live MCP bridge dispatch routing ─────────────────────────────────
def test_bridge_routes_agent_loop_tools_to_live_agent(monkeypatch):
    from agent.transports import claude_live_tools_mcp as mod

    calls = {}

    def _fake_todo(todos=None, merge=False, store=None):
        calls["todo"] = (todos, merge, store)
        return "todo-ok"

    monkeypatch.setitem(__import__("sys").modules, "tools.todo_tool",
                        SimpleNamespace(todo_tool=_fake_todo))

    agent = SimpleNamespace(_todo_store="STORE", session_id="s1")
    bridge = mod.LiveToolsMCPServer(agent=agent)
    out = bridge._dispatch_tool("todo", {"todos": [{"t": "x"}], "merge": True})
    assert out == "todo-ok"
    assert calls["todo"] == ([{"t": "x"}], True, "STORE")


def test_bridge_routes_stateless_tools_to_handle_function_call(monkeypatch):
    from agent.transports import claude_live_tools_mcp as mod

    seen = {}

    def _fake_hfc(name, args, **kwargs):
        seen["name"] = name
        seen["args"] = args
        return "hfc-ok"

    monkeypatch.setitem(__import__("sys").modules, "model_tools",
                        SimpleNamespace(handle_function_call=_fake_hfc))

    bridge = mod.LiveToolsMCPServer(agent=None)
    out = bridge._dispatch_tool("web_search", {"query": "x"})
    assert out == "hfc-ok"
    assert seen["name"] == "web_search" and seen["args"] == {"query": "x"}


def test_bridge_tool_error_is_caught(monkeypatch):
    from agent.transports import claude_live_tools_mcp as mod

    def _boom(name, args, **kwargs):
        raise ValueError("kaboom")

    monkeypatch.setitem(__import__("sys").modules, "model_tools",
                        SimpleNamespace(handle_function_call=_boom))
    bridge = mod.LiveToolsMCPServer(agent=None)
    out = bridge._dispatch_tool("web_search", {"query": "x"})
    data = json.loads(out)
    assert data["tool"] == "web_search" and "kaboom" in data["error"]


# ── provider registration + factory + runtime resolution ─────────────
def test_provider_registered_in_auth_registry():
    from hermes_cli.auth import PROVIDER_REGISTRY
    assert "claude-cli" in PROVIDER_REGISTRY
    assert PROVIDER_REGISTRY["claude-cli"].auth_type == "external_process"


def test_runtime_resolves_claude_cli():
    from hermes_cli.runtime_provider import resolve_runtime_provider
    r = resolve_runtime_provider(requested="claude-cli")
    assert r["provider"] == "claude-cli"
    assert r["api_mode"] == "chat_completions"
    assert r["base_url"] == "claude-cli://local"


def test_extra_usage_400_is_non_retryable_with_actionable_message():
    from agent.error_classifier import classify_api_error, FailoverReason

    class _FakeAnthropic400(Exception):
        status_code = 400

    err = _FakeAnthropic400(
        "Error code: 400 - Third-party apps now draw from your extra usage, "
        "not your plan limits. Add more at claude.ai/settings/usage and keep going."
    )
    classified = classify_api_error(err, provider="anthropic", model="claude-opus-4-8")
    assert classified.reason == FailoverReason.billing
    assert classified.retryable is False
    assert "claude-cli" in classified.message


def test_factory_branch_selects_claude_cli_client():
    import agent.agent_runtime_helpers as arh

    agent = SimpleNamespace(
        provider="claude-cli",
        base_url="claude-cli://local",
        enabled_toolsets=None, disabled_toolsets=None, _current_task_id=None,
        session_id="s1",
        _client_log_context=lambda: "ctx",
        _build_keepalive_http_client=lambda u: None,
    )
    client = arh.create_openai_client(
        agent, {"base_url": "claude-cli://local", "api_key": "x", "model": "haiku"},
        reason="test", shared=False,
    )
    assert type(client).__name__ == "ClaudeCLIClient"
    assert hasattr(client.chat.completions, "create")
    client.close()
