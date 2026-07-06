"""Lightweight shell execution for the CLI ``!`` command.

Executes via ``subprocess.run()`` directly — no Docker/SSH/Modal backends,
no managed-process lifecycle, no approval callbacks.

Public API:
    execute_shell_command(command, timeout=30, cwd=None) -> dict
"""

from __future__ import annotations

import logging
import os
import platform
import signal
import subprocess
import sys
from typing import Any

from tools.tool_output_limits import get_max_bytes

logger = logging.getLogger(__name__)

# ── Internal helpers ─────────────────────────────────────────────────


def _resolve_cwd(cwd: str | None) -> str:
    """Resolve a safe working directory, walking up ancestors if needed."""
    if cwd is None:
        cwd = os.getcwd()
    # Walk up ancestors if the current cwd no longer exists (e.g. a deleted
    # tempdir).  Mirror the pattern from tools/environments/local.py.
    candidate = os.path.abspath(cwd)
    for _ in range(256):  # safety limit vs infinite loop
        if os.path.isdir(candidate):
            return candidate
        parent = os.path.dirname(candidate)
        if parent == candidate:
            break
        candidate = parent
    return os.path.expanduser("~")  # ultimate fallback


def _truncate_output(output: str, max_bytes: int | None = None) -> str:
    """Truncate *output* to *max_bytes* and append a note."""
    if max_bytes is None:
        max_bytes = get_max_bytes()
    if len(output.encode("utf-8")) <= max_bytes:
        return output
    # Cut at byte boundary then re-decode cleanly
    truncated = output.encode("utf-8")[:max_bytes].decode("utf-8", errors="replace")
    truncated += (
        f"\n\n[... output truncated at {max_bytes} bytes ({len(output)} total) ...]"
    )
    return truncated


def _try_strip_ansi(text: str) -> str:
    """Strip ANSI escape codes, best-effort."""
    try:
        from tools.ansi_strip import strip_ansi

        return strip_ansi(text)
    except ImportError:
        return text


# ── Public API ───────────────────────────────────────────────────────


def execute_shell_command(
    command: str,
    timeout: int = 30,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Run *command* in a local shell and return its output.

    Args:
        command: Shell command string (without the leading ``!``).
        timeout: Maximum wall-clock seconds before the process is
            killed (default 30).  Set to 0 for no timeout.
        cwd: Working directory (defaults to ``os.getcwd()``).

    Returns:
        dict with keys:

        * ``output`` (str) — merged stdout+stderr, ANSI-stripped,
          possibly truncated.
        * ``exit_code`` (int) — exit code, or -1 when timed out or
          an OSError prevented execution.
        * ``error`` (str | None) — human-readable error description
          when the command could not be started or timed out.
        * ``timed_out`` (bool) — ``True`` when the timeout was reached.
    """
    if not command or not command.strip():
        return {
            "output": "",
            "exit_code": 0,
            "error": None,
            "timed_out": False,
        }

    cwd = _resolve_cwd(cwd)
    _timeout: float | None = float(timeout) if timeout and timeout > 0 else None

    # Platform-specific setup
    is_posix = platform.system() != "Windows"
    kwargs: dict[str, Any] = {
        "args": command,
        "shell": True,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }

    if is_posix:
        kwargs["executable"] = "/bin/bash"
        kwargs["preexec_fn"] = os.setsid  # isolate process group
    else:
        # Windows: prevent the console window from flashing
        kwargs.setdefault("creationflags", 0)
        try:
            kwargs["creationflags"] |= subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        except AttributeError:
            pass

    try:
        proc = subprocess.Popen(
            command,
            **{k: v for k, v in kwargs.items() if k != "args"},
            cwd=cwd,
        )
        stdout_bytes, _ = proc.communicate(timeout=_timeout)
        raw_output = stdout_bytes or ""
        exit_code: int = proc.poll() or 0
        error: str | None = None
        timed_out: bool = False
    except subprocess.TimeoutExpired:
        # Kill the entire process group so orphaned children don't linger
        if is_posix and proc.pid is not None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        proc.kill()
        proc.wait(timeout=5)
        raw_output = ""
        try:
            stdout_bytes, _ = proc.communicate(timeout=2)
            raw_output = stdout_bytes or ""
        except (subprocess.TimeoutExpired, ValueError):
            pass
        exit_code = -1
        error = f"Command timed out after {timeout} seconds"
        timed_out = True
    except FileNotFoundError:
        raw_output = ""
        exit_code = 127
        error = "Command not found"
        timed_out = False
    except OSError as exc:
        raw_output = ""
        exit_code = -1
        error = str(exc)
        timed_out = False

    # Clean up: strip ANSI, truncate
    output = _try_strip_ansi(raw_output)
    output = _truncate_output(output)

    return {
        "output": output,
        "exit_code": exit_code,
        "error": error,
        "timed_out": timed_out,
    }
