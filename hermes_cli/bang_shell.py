"""Direct shell escapes for the interactive CLI.

Typing ``!git status`` runs one command in the session's working directory;
typing a bare ``!`` hands the terminal to the user's shell until that shell
exits. Both paths inherit the real terminal, so OAuth logins, pagers, REPLs,
and other TTY-dependent programs work normally.

The model is never invoked: no user message, assistant message, or tool result
enters conversation history. One-shot commands still pass through the terminal
tool's dangerous-pattern approval gate. A bare ``!`` is an explicit escape
into the user's own shell, so commands entered there are not mediated by
Hermes.

CLI-only by design: gateway/API/cron sessions have their own shells and no
composer, so :func:`bang_shell_enabled` gates the feature off there.
"""

from __future__ import annotations

import os
import subprocess
from contextlib import suppress
from typing import Optional


# Interactive convenience, not agent work: keep the ceiling well under the terminal tool's foreground
# cap so an accidental `!sleep 999` cannot wedge the composer (the user can Ctrl+C anyway).
DEFAULT_TIMEOUT = 120

# Legacy facade helpers kept for external callers while the interactive implementation lives here.
# They describe the pre-extraction, agent-context shell path; current bang execution intentionally
# bypasses the model and uses the functions below instead.
_SHELL_DISPLAY_MAX = 1000


def _looks_like_shell_command(text: str) -> bool:
    """Return whether *text* has the legacy leading-bang shell form."""
    return isinstance(text, str) and text.startswith("!")


def _format_shell_output(command: str, result: dict) -> str:
    """Format a legacy shell-executor result for callers that still consume it."""
    exit_code = result.get("exit_code", -1)
    output = result.get("output", "")
    error = result.get("error")
    timed_out = result.get("timed_out", False)
    if error:
        header = f"[Shell command executed: {command} (ERROR: {error})]"
    elif timed_out:
        header = f"[Shell command executed: {command} (TIMED OUT)]"
    elif exit_code != 0:
        header = f"[Shell command executed: {command} (exit_code={exit_code})]"
    else:
        header = f"[Shell command executed: {command}]"
    return f"{header}\n{output}".strip()


def _shell_display_truncate(text: str) -> str:
    """Limit legacy shell display text without changing its model-facing value."""
    if len(text) <= _SHELL_DISPLAY_MAX:
        return text
    return text[:_SHELL_DISPLAY_MAX - 1].rstrip() + "…"


def is_bang_command(text: Optional[str]) -> bool:
    """True when *text* is a ``!`` shell-mode submission.

    Only a leading ``!`` (after surrounding whitespace) counts; ``fix the bug!`` is an ordinary
    prompt and must reach the agent untouched.
    """
    return isinstance(text, str) and text.strip().startswith("!")


def parse_bang_command(text: str) -> str:
    """The shell command inside a bang submission (``""`` when bare).

    ``!  ls -la`` -> ``ls -la``; ``!!`` -> ``!`` — a literal second bang belongs to the user's shell
    (history expansion), not to Hermes.
    """
    return text.strip()[1:].strip() if is_bang_command(text) else ""


def bang_shell_enabled() -> bool:
    """True only for interactive local CLI sessions.

    Gateway, API, and cron sessions never reach the composer and their users already have a shell;
    running arbitrary commands for them would be a remote-execution surface with no approving human
    at the keyboard.
    """
    try:
        from utils import env_var_enabled
    except Exception:  # pragma: no cover - utils is always importable in-tree
        def env_var_enabled(name, default=""):  # type: ignore[misc]
            return str(os.getenv(name, default)).strip().lower() in {"1", "true", "yes", "on"}

    return not (env_var_enabled("HERMES_GATEWAY_SESSION") or env_var_enabled("HERMES_CRON_SESSION")
                or (os.getenv("HERMES_SESSION_PLATFORM") or "").strip())


def resolve_bang_cwd(session_key: Optional[str] = None) -> Optional[str]:
    """The directory a bang command should run in.

    Mirrors the terminal tool's order so ``!pwd`` matches the agent's own commands: the session's
    recorded ``cd`` state first, then the configured ``TERMINAL_CWD``/backend default.
    """
    try:
        from tools.terminal_tool import _get_env_config, get_session_cwd
        return get_session_cwd(session_key) or (_get_env_config() or {}).get("cwd") or None
    except Exception:
        return None


def check_bang_approval(command: str) -> dict:
    """Run *command* through the terminal tool's approval gate.

    Reuses ``tools.terminal_tool._check_all_guards`` — exactly what ``terminal_tool()`` calls — so the
    hardline blocklist, user deny rules, tirith findings, and the dangerous-command prompt all apply
    to user-typed bang commands too. Returns the gate's ``{"approved": bool, "message": ...}``;
    falls back to *approved* only when the gate itself cannot be imported (a broken install, not a
    policy decision).
    """
    try:
        from tools.terminal_tool import _check_all_guards
    except Exception:
        return {"approved": True, "message": None}

    # Bang commands always run locally in the CLI process, never inside a remote/sandbox backend.
    return _check_all_guards(command, "local", has_host_access=False)


def _bang_env() -> dict:
    """Environment for a bang command with Hermes-managed secrets filtered.

    The CLI process holds every provider API key; a user-typed command may still run a third-party
    script, so reuse the sanitizer ``quick_commands`` and the local terminal backend use.
    """
    try:
        from tools.environments.local import build_subprocess_env
        return build_subprocess_env()  # == _sanitize_subprocess_env(os.environ.copy())
    except Exception:
        return os.environ.copy()  # tools package unimportable: run the user's command anyway


def resolve_interactive_shell() -> list[str]:
    """Return the user's interactive shell without involving ``shell=True``."""
    if os.name == "nt":
        return [os.environ.get("COMSPEC") or "cmd.exe"]
    return [os.environ.get("SHELL") or "/bin/sh"]


async def run_bang_command_interactive(
    app,
    command: Optional[str],
    *,
    cwd: Optional[str] = None,
) -> int:
    """Run a command, or the user's shell, on prompt_toolkit's real terminal."""
    import sys

    from prompt_toolkit.application import in_terminal
    from prompt_toolkit.eventloop import run_in_executor_with_context

    run_cwd = cwd if (cwd and os.path.isdir(os.path.expanduser(cwd))) else None
    if run_cwd:
        run_cwd = os.path.expanduser(run_cwd)

    try:
        input_fd = app.input.fileno()
    except (AttributeError, OSError):
        input_fd = sys.stdin.fileno()
    try:
        output_fd = app.output.fileno()
    except (AttributeError, OSError):
        output_fd = sys.stdout.fileno()

    def run() -> int:
        try:
            if command is None:
                proc = subprocess.Popen(
                    resolve_interactive_shell(),
                    stdin=input_fd,
                    stdout=output_fd,
                    stderr=output_fd,
                    cwd=run_cwd,
                    env=_bang_env(),
                )
            else:
                proc = subprocess.Popen(
                    command,
                    shell=True,
                    stdin=input_fd,
                    stdout=output_fd,
                    stderr=output_fd,
                    cwd=run_cwd,
                    env=_bang_env(),
                )
            return int(proc.wait() or 0)
        except OSError:
            return 127

    async with in_terminal():
        return await run_in_executor_with_context(run)


def run_bang_command(
    command: str,
    *,
    cwd: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    writer=None,
) -> int:
    """Execute *command* and stream its output, returning the exit code.

    Output exists only on the user's terminal — nothing is returned for insertion into history.
    """
    emit = writer or (lambda line: print(line, end="" if line.endswith("\n") else "\n"))
    run_cwd = os.path.expanduser(cwd) if cwd else None
    if run_cwd and not os.path.isdir(run_cwd):
        run_cwd = None
    try:
        from hermes_cli._subprocess_compat import windows_hide_flags
        creationflags = windows_hide_flags()
    except Exception:
        creationflags = 0
    try:
        # shell=True is intentional (matches quick_commands): the human typed this, not the model.
        proc = subprocess.Popen(
            command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", cwd=run_cwd, env=_bang_env(),
            creationflags=creationflags)
    except Exception as exc:
        emit(f"!: failed to run command: {exc}")
        return 127
    try:
        if proc.stdout is not None:
            for line in proc.stdout:
                emit(line.rstrip("\n"))
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        emit(f"!: command timed out after {timeout}s")
        return 124
    except KeyboardInterrupt:
        # Ctrl+C interrupts the command, not the Hermes session.
        proc.kill()
        emit("!: interrupted")
        return 130
    finally:
        if proc.stdout is not None:
            with suppress(Exception):
                proc.stdout.close()
    return int(proc.returncode or 0)
