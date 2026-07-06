"""Tests for tools.shell_executor — the ``!`` command backend."""

import signal
import time

import pytest

from tools.shell_executor import execute_shell_command


class TestBasicExecution:
    """Shell command execution basics."""

    def test_echo(self):
        result = execute_shell_command("echo hello", timeout=5)
        assert result["exit_code"] == 0
        assert "hello" in result["output"]
        assert result["error"] is None
        assert result["timed_out"] is False

    def test_nonzero_exit(self):
        result = execute_shell_command("false", timeout=5)
        assert result["exit_code"] == 1
        assert result["error"] is None
        assert result["timed_out"] is False

    def test_empty_command(self):
        """Empty or whitespace-only command succeeds with empty output."""
        result = execute_shell_command("", timeout=5)
        assert result["exit_code"] == 0
        assert result["output"] == ""
        assert result["error"] is None

        result = execute_shell_command("   ", timeout=5)
        assert result["exit_code"] == 0
        assert result["output"] == ""

    def test_command_not_found(self):
        """Non-existent command yields exit code 127 (shell reports in output)."""
        result = execute_shell_command("xyznonexistent99", timeout=5)
        assert result["exit_code"] == 127
        # The shell's "not found" message appears in the output stream,
        # not as the error field (that's reserved for subprocess-level failures).
        assert result["timed_out"] is False


class TestTimeout:
    """Timeout behavior."""

    def test_timeout_kills_process(self):
        """A command that exceeds the timeout should be killed."""
        result = execute_shell_command("sleep 10", timeout=1)
        assert result["timed_out"] is True
        assert result["exit_code"] == -1
        assert "timed out" in (result["error"] or "").lower()

    def test_zero_timeout_no_timeout(self):
        """timeout=0 means no timeout — command completes normally."""
        result = execute_shell_command("echo no-timeout", timeout=0)
        assert result["exit_code"] == 0
        assert result["timed_out"] is False
        assert "no-timeout" in result["output"]

    def test_timeout_with_partial_output(self):
        """A command producing output before a timeout should return partial output."""
        result = execute_shell_command(
            "echo before && sleep 10", timeout=2
        )
        assert result["timed_out"] is True
        assert "before" in (result["output"] or "")


class TestOutputHandling:
    """Stdout/stderr merging and cleanup."""

    def test_stderr_included_in_output(self):
        """stderr is merged into the output field."""
        result = execute_shell_command("echo out && echo err >&2", timeout=5)
        assert result["exit_code"] == 0
        assert "out" in result["output"]
        assert "err" in result["output"]

    def test_multiline_output(self):
        """Multi-line output is preserved verbatim."""
        result = execute_shell_command(
            "echo line1 && echo line2 && echo line3", timeout=5
        )
        assert "line1" in result["output"]
        assert "line2" in result["output"]
        assert "line3" in result["output"]

    def test_truncation(self):
        """Very large output is truncated (uses get_max_bytes)."""
        # Generate ~100KB of output (default limit is 50KB)
        result = execute_shell_command(
            "python3 -c 'print(\"x\" * 100000)'", timeout=5
        )
        assert result["exit_code"] == 0
        # Should be truncated
        output_len = len(result["output"])
        assert output_len < 60_000, (
            f"Expected truncated output (<60000 bytes) but got {output_len} bytes"
        )
        assert "truncated" in result["output"].lower()


class TestCwdHandling:
    """Working directory resolution."""

    def test_cwd_respected(self):
        """Command runs in the specified cwd."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            result = execute_shell_command("pwd", timeout=5, cwd=tmpdir)
            assert tmpdir in result["output"]

    def test_missing_cwd_falls_back(self):
        """Missing cwd falls back to an ancestor or home."""
        result = execute_shell_command("echo ok", timeout=5, cwd="/nonexistent_path_xyz")
        assert result["exit_code"] == 0
        assert "ok" in result["output"]


class TestSignalIsolation:
    """Process group isolation."""

    @staticmethod
    def _count_sleep_processes() -> int:
        """Count `sleep` processes owned by this test's user, best-effort."""
        import os
        import subprocess

        try:
            pid = os.getpid()
            result = subprocess.run(
                ["pgrep", "-P", str(pid), "sleep"],
                capture_output=True, text=True, timeout=3,
            )
            if result.returncode == 0 and result.stdout.strip():
                return len(result.stdout.strip().splitlines())
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
        return 0

    def test_no_orphans_after_timeout(self):
        """A timed-out subprocess should have no orphaned children."""
        import os

        before = self._count_sleep_processes()
        execute_shell_command("sleep 20", timeout=1)
        # Small delay to let signals propagate
        time.sleep(1)
        after = self._count_sleep_processes()
        # Assert no new sleep processes leaked
        assert after <= before, (
            f"Expected no orphan sleep processes (before={before}, after={after})"
        )
