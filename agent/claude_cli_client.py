"""OpenAI-compatible shim that runs Hermes turns through the local `claude` CLI.

Why
---
Hermes' native Anthropic path sends the Claude Code OAuth token to
``api.anthropic.com`` directly. Anthropic now bills such third-party API calls
against a separately-funded "extra usage" balance, not the Pro/Max plan
(``HTTP 400: Third-party apps now draw from your extra usage…``). The only way
to draw from plan limits is to let the genuine first-party ``claude`` binary
make the request.

This client therefore spawns ``claude -p`` per turn (like
:class:`agent.copilot_acp_client.CopilotACPClient` spawns ``copilot --acp``),
and exposes Hermes' FULL tool surface to that subprocess via an in-process MCP
bridge (:class:`agent.transports.claude_live_tools_mcp.LiveToolsMCPServer`) so
the Claude turn is not limited to its own shell — every Hermes tool, including
the agent-loop tools (todo/memory/session_search/delegate_task), is callable.

Auth: the subprocess gets a scrubbed env carrying the resolved
``CLAUDE_CODE_OAUTH_TOKEN`` and ``CLAUDECODE=""``; inherited
``ANTHROPIC_API_KEY`` / ``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_AUTH_TOKEN`` are
NOT forwarded (they would override OAuth and 401), and ``--bare`` is never used
(it forces API-key auth and ignores OAuth).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

logger = logging.getLogger(__name__)

CLAUDE_CLI_MARKER_BASE_URL = "claude-cli://local"
_DEFAULT_TIMEOUT_SECONDS = 1800.0

# Env vars that must NOT leak into the subprocess: they would override the
# OAuth token and make the first-party binary authenticate as something else
# (observed: inherited proxy creds cause "401 Invalid authentication
# credentials").
_AUTH_ENV_BLOCKLIST = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
)
# Harness vars that confuse a nested invocation (set by a parent Claude Code).
_PASSTHROUGH_ENV = ("HOME", "PATH", "USER", "LANG", "LC_ALL", "TMPDIR", "TMP", "TEMP", "SHELL")


def _resolve_command() -> str:
    return os.getenv("HERMES_CLAUDE_CLI_COMMAND", "").strip() or "claude"


def _render_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                txt = item.get("text")
                if isinstance(txt, str) and txt.strip():
                    parts.append(txt.strip())
        return "\n".join(parts).strip()
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"].strip()
        return json.dumps(content, ensure_ascii=True)
    return str(content).strip()


def _split_system_and_transcript(messages: list[dict[str, Any]]) -> tuple[str, str]:
    """Return (system_prompt, transcript) from an OpenAI-format message list.

    System messages are concatenated into the system prompt (passed via
    ``--append-system-prompt`` so Hermes' persona/instructions carry over);
    everything else becomes a labelled transcript used as the ``-p`` prompt.
    """
    system_parts: list[str] = []
    transcript: list[str] = []
    label = {"user": "User", "assistant": "Assistant", "tool": "Tool result"}
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").strip().lower()
        rendered = _render_content(msg.get("content"))
        # Surface assistant tool calls so context isn't lost across turns.
        if role == "assistant" and msg.get("tool_calls"):
            try:
                calls = ", ".join(
                    (tc.get("function", {}) or {}).get("name", "?")
                    for tc in msg["tool_calls"] if isinstance(tc, dict)
                )
                rendered = (rendered + f"\n[called tools: {calls}]").strip()
            except Exception:
                pass
        if not rendered:
            continue
        if role == "system":
            system_parts.append(rendered)
        else:
            transcript.append(f"{label.get(role, role.title())}:\n{rendered}")
    return "\n\n".join(system_parts).strip(), "\n\n".join(transcript).strip()


class _Completions:
    def __init__(self, client: "ClaudeCLIClient"):
        self._client = client

    def create(self, **kwargs: Any) -> Any:
        return self._client._create_chat_completion(**kwargs)


class _Chat:
    def __init__(self, client: "ClaudeCLIClient"):
        self.completions = _Completions(client)


class ClaudeCLIClient:
    """Minimal OpenAI-client-compatible facade over the `claude` CLI."""

    def __init__(
        self,
        *,
        agent: Any = None,
        api_key: str | None = None,
        base_url: str | None = None,
        default_headers: dict[str, str] | None = None,
        timeout: Any = None,
        **_: Any,
    ) -> None:
        self._agent = agent
        self.api_key = api_key or "claude-cli"
        self.base_url = base_url or CLAUDE_CLI_MARKER_BASE_URL
        self._command = _resolve_command()
        self._default_timeout = self._coerce_timeout(timeout)
        self.chat = _Chat(self)
        self.is_closed = False

        self._bridge = None  # LiveToolsMCPServer
        self._bridge_lock = threading.Lock()
        self._token: Optional[str] = None
        self._active_process: Optional[subprocess.Popen] = None
        self._proc_lock = threading.Lock()

    # ── helpers ─────────────────────────────────────────────────────
    @staticmethod
    def _coerce_timeout(timeout: Any) -> float:
        if timeout is None:
            return _DEFAULT_TIMEOUT_SECONDS
        if isinstance(timeout, (int, float)):
            return float(timeout) if timeout > 0 else _DEFAULT_TIMEOUT_SECONDS
        candidates = [getattr(timeout, a, None) for a in ("read", "write", "connect", "pool", "timeout")]
        numeric = [float(v) for v in candidates if isinstance(v, (int, float)) and v > 0]
        return max(numeric) if numeric else _DEFAULT_TIMEOUT_SECONDS

    def _resolve_token(self) -> str:
        if self._token:
            return self._token
        token = ""
        try:
            from agent.anthropic_adapter import resolve_anthropic_token
            token = (resolve_anthropic_token() or "").strip()
        except Exception:
            token = ""
        if not token:
            token = (os.getenv("CLAUDE_CODE_OAUTH_TOKEN") or os.getenv("ANTHROPIC_TOKEN") or "").strip()
        self._token = token
        return token

    def _ensure_bridge(self):
        with self._bridge_lock:
            if self._bridge is not None:
                return self._bridge
            from agent.transports.claude_live_tools_mcp import LiveToolsMCPServer
            self._bridge = LiveToolsMCPServer(
                agent=self._agent,
                enabled_toolsets=getattr(self._agent, "enabled_toolsets", None),
                disabled_toolsets=getattr(self._agent, "disabled_toolsets", None),
                task_id=getattr(self._agent, "_current_task_id", None),
            )
            # Surface tool invocations to the terminal so users see progress.
            _vprint = getattr(self._agent, "_vprint", None) if self._agent else None
            if callable(_vprint):
                def _on_tool_call(name: str, args: dict) -> None:
                    try:
                        preview = json.dumps(args, ensure_ascii=False)
                        if len(preview) > 120:
                            preview = preview[:117] + "…"
                        _vprint(f"  ↳ {name}({preview})", force=True)
                    except Exception:
                        pass
                self._bridge.on_tool_call = _on_tool_call
            self._bridge.start()
            return self._bridge

    def _build_env(self, token: str) -> dict[str, str]:
        env: dict[str, str] = {}
        for key in _PASSTHROUGH_ENV:
            val = os.environ.get(key)
            if val:
                env[key] = val
        env.setdefault("HOME", os.path.expanduser("~"))
        env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
        # First-party OAuth auth; scrub anything that would override it.
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        env["CLAUDECODE"] = ""  # avoid nested-session confusion
        for blocked in _AUTH_ENV_BLOCKLIST:
            env.pop(blocked, None)
        return env

    def _cwd(self) -> str:
        for attr in ("cwd", "_cwd", "working_dir"):
            val = getattr(self._agent, attr, None)
            if isinstance(val, str) and val.strip():
                try:
                    return str(Path(val).resolve())
                except Exception:
                    pass
        return os.getcwd()

    # ── main entry point ────────────────────────────────────────────
    def _create_chat_completion(
        self,
        *,
        model: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        timeout: Any = None,
        **_: Any,
    ) -> Any:
        token = self._resolve_token()
        bridge = self._ensure_bridge()
        system_prompt, transcript = _split_system_and_transcript(messages or [])
        prompt = transcript or "Continue."

        mcp_config = json.dumps(
            {"mcpServers": {"hermes": {"type": "http", "url": bridge.url}}}
        )
        model_name = (model or "").split("/")[-1] or "sonnet"

        cmd = [
            self._command, "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--model", model_name,
            "--mcp-config", mcp_config,
            "--strict-mcp-config",
            "--permission-mode", "bypassPermissions",
        ]
        if system_prompt:
            cmd += ["--append-system-prompt", system_prompt]

        effective_timeout = self._coerce_timeout(timeout) if timeout is not None else self._default_timeout

        # Resolve a stream callback so partial text appears as it's generated.
        # _stream_callback(text, end="") is the hermes typewriter hook; we also
        # accept _vprint for simpler callers (e.g. tests, gateway sessions).
        _stream_cb = None
        if self._agent is not None:
            _stream_cb = getattr(self._agent, "_stream_callback", None)
            if not callable(_stream_cb):
                _stream_cb = None

        result_text, usage = self._run(cmd, token, effective_timeout, stream_cb=_stream_cb)

        usage_ns = SimpleNamespace(
            prompt_tokens=usage.get("input_tokens", 0) or 0,
            completion_tokens=usage.get("output_tokens", 0) or 0,
            total_tokens=(usage.get("input_tokens", 0) or 0) + (usage.get("output_tokens", 0) or 0),
            prompt_tokens_details=SimpleNamespace(
                cached_tokens=usage.get("cache_read_input_tokens", 0) or 0
            ),
        )
        message = SimpleNamespace(
            content=result_text,
            tool_calls=[],  # Claude executed tools itself via the MCP bridge
            reasoning=None,
            reasoning_content=None,
            reasoning_details=None,
        )
        choice = SimpleNamespace(message=message, finish_reason="stop")
        return SimpleNamespace(choices=[choice], usage=usage_ns, model=model_name)

    # ── terminal 400/401 errors that must NOT be retried ────────────
    _TERMINAL_STATUSES = frozenset({400, 401, 403})

    def _run(
        self,
        cmd: list[str],
        token: str,
        timeout_seconds: float,
        *,
        stream_cb=None,
        max_attempts: int = 3,
    ) -> tuple[str, dict]:
        """Spawn the claude CLI, parse stream-json NDJSON, retry on transient failures.

        ``stream_cb``: if provided, called with each partial text chunk so the
        user sees output as it arrives rather than waiting for the full turn.
        Signature: ``stream_cb(text: str) -> None``.
        """
        last_exc: Exception | None = None
        for attempt in range(max_attempts):
            if attempt:
                delay = min(2.0 * attempt, 8.0)
                logger.info("claude CLI attempt %d/%d (retrying in %.1fs)", attempt + 1, max_attempts, delay)
                import time as _time
                _time.sleep(delay)
            try:
                result_text, usage = self._run_once(cmd, token, timeout_seconds, stream_cb=stream_cb)
                return result_text, usage
            except RuntimeError as exc:
                msg = str(exc)
                # Terminal errors: auth, policy, bad-request — don't retry
                if any(f"api_status={s}" in msg for s in self._TERMINAL_STATUSES):
                    raise
                if "extra usage" in msg.lower() and "plan limits" in msg.lower():
                    raise
                last_exc = exc
                logger.warning("claude CLI attempt %d/%d failed: %s", attempt + 1, max_attempts, msg[:160])
            except TimeoutError as exc:
                last_exc = exc
                logger.warning("claude CLI attempt %d/%d timed out", attempt + 1, max_attempts)

        raise last_exc or RuntimeError("claude CLI: all retry attempts exhausted")

    def _run_once(
        self,
        cmd: list[str],
        token: str,
        timeout_seconds: float,
        *,
        stream_cb=None,
    ) -> tuple[str, dict]:
        env = self._build_env(token)
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, cwd=self._cwd(), env=env,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Could not start the '{self._command}' CLI. Install Claude Code "
                "(npm install -g @anthropic-ai/claude-code) or set "
                "HERMES_CLAUDE_CLI_COMMAND."
            ) from exc

        with self._proc_lock:
            self._active_process = proc

        text_parts: list[str] = []
        usage: dict = {}
        last_error: Optional[str] = None
        stderr_lines: list[str] = []

        # Read stderr in a background thread to prevent blocking.
        def _read_stderr():
            if proc.stderr:
                for line in proc.stderr:
                    stderr_lines.append(line.rstrip("\n"))

        import threading as _threading
        _err_thread = _threading.Thread(target=_read_stderr, daemon=True)
        _err_thread.start()

        import time as _time
        deadline = _time.monotonic() + timeout_seconds
        try:
            for raw_line in proc.stdout:
                if _time.monotonic() > deadline:
                    proc.kill()
                    raise TimeoutError(f"claude CLI timed out after {int(timeout_seconds)}s")

                line = raw_line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type", "")

                # Partial / full assistant message — stream text to the UI.
                if etype == "assistant":
                    msg_content = (event.get("message") or {}).get("content") or []
                    if isinstance(msg_content, list):
                        for block in msg_content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                chunk = block.get("text") or ""
                                if chunk:
                                    text_parts.append(chunk)
                                    if stream_cb is not None:
                                        try:
                                            stream_cb(chunk)
                                        except Exception:
                                            pass

                # Tool result (user turn) — log which tool just ran.
                elif etype == "user":
                    msg_content = (event.get("message") or {}).get("content") or []
                    if isinstance(msg_content, list):
                        for block in msg_content:
                            if isinstance(block, dict) and block.get("type") == "tool_result":
                                tool_name = block.get("tool_use_id", "tool")
                                logger.debug("claude CLI tool result: %s", tool_name)

                # Final result — collect usage and check for errors.
                elif etype == "result":
                    usage_raw = event.get("usage")
                    if isinstance(usage_raw, dict):
                        usage = usage_raw
                    if event.get("is_error") or event.get("subtype") not in (None, "success"):
                        status = event.get("api_error_status")
                        last_error = (
                            f"claude CLI error (api_status={status}): "
                            f"{event.get('result') or event.get('subtype')}"
                        )
                    break

        except TimeoutError:
            raise
        finally:
            proc.stdout.close()
            _err_thread.join(timeout=2.0)
            with self._proc_lock:
                self._active_process = None

        proc.wait()

        if last_error:
            raise RuntimeError(last_error)

        result_text = "".join(text_parts).strip()
        if not result_text and not usage:
            stderr_tail = "\n".join(stderr_lines[-10:]).strip()
            raise RuntimeError(
                f"claude CLI produced no output (exit {proc.returncode}). "
                f"stderr: {stderr_tail[:400]}"
            )

        return result_text, usage

    def close(self) -> None:
        self.is_closed = True
        with self._proc_lock:
            proc = self._active_process
            self._active_process = None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        with self._bridge_lock:
            bridge = self._bridge
            self._bridge = None
        if bridge is not None:
            try:
                bridge.stop()
            except Exception:
                pass
