"""Tests for CLI ``!`` shell command detection and output formatting."""

import pytest


class TestLooksLikeShellCommand:
    """``_looks_like_shell_command`` input detection."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from cli import _looks_like_shell_command

        self._detect = _looks_like_shell_command

    def test_basic(self):
        assert self._detect("!ls -la") is True

    def test_bare_bang(self):
        """Bare ! is detected — handled by _handle_shell_command as usage hint."""
        assert self._detect("!") is True

    def test_only_whitespace_after_bang(self):
        """! followed by whitespace only still detected (bare-bang handling)."""
        assert self._detect("!   ") is True

    def test_not_a_shell_command(self):
        assert self._detect("normal text") is False
        assert self._detect("/help") is False
        assert self._detect("!notalone") is True  # embedded bang is NOT a command

    def test_empty_string(self):
        assert self._detect("") is False

    def test_non_string(self):
        assert self._detect(None) is False  # type: ignore[arg-type]

    def test_integer_input(self):
        assert self._detect(123) is False  # type: ignore[arg-type]

    def test_shell_command_with_slash(self):
        """! followed by a path should still be detected."""
        assert self._detect("!/usr/bin/echo hello") is True


class TestFormatShellOutput:
    """``_format_shell_output`` result formatting."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from cli import _format_shell_output

        self._fmt = _format_shell_output

    def test_format_success(self):
        result = {
            "output": "hello\nworld",
            "exit_code": 0,
            "error": None,
            "timed_out": False,
        }
        formatted = self._fmt("echo hello", result)
        assert "[Shell command executed: echo hello]" in formatted
        assert "hello\nworld" in formatted

    def test_format_nonzero_exit(self):
        result = {
            "output": "",
            "exit_code": 1,
            "error": None,
            "timed_out": False,
        }
        formatted = self._fmt("false", result)
        assert "exit_code=1" in formatted
        assert "[Shell command executed: false" in formatted

    def test_format_timed_out(self):
        """When error is set (timeout produces an error), the error header wins."""
        result = {
            "output": "partial output",
            "exit_code": -1,
            "error": "Command timed out after 1 seconds",
            "timed_out": True,
        }
        formatted = self._fmt("sleep 10", result)
        assert "ERROR: Command timed out" in formatted
        assert "partial output" in formatted

    def test_format_timed_out_no_error(self):
        """Without an error field, TIMED OUT header is used."""
        result = {
            "output": "",
            "exit_code": -1,
            "error": None,
            "timed_out": True,
        }
        formatted = self._fmt("sleep 10", result)
        assert "TIMED OUT" in formatted

    def test_format_error(self):
        result = {
            "output": "",
            "exit_code": 127,
            "error": "Command not found",
            "timed_out": False,
        }
        formatted = self._fmt("nonexistent", result)
        assert "ERROR: Command not found" in formatted

    def test_format_success_no_output(self):
        """A command that succeeds with no output should produce a minimal header."""
        result = {
            "output": "",
            "exit_code": 0,
            "error": None,
            "timed_out": False,
        }
        formatted = self._fmt("true", result)
        assert "[Shell command executed: true]" in formatted


class TestShellDisplayTruncate:
    """``_shell_display_truncate`` TUI display cap — full text still goes to LLM."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from cli import _shell_display_truncate, _SHELL_DISPLAY_MAX

        self._truncate = _shell_display_truncate
        self._max = _SHELL_DISPLAY_MAX

    def test_short_passthrough(self):
        """Text under the cap is returned unchanged."""
        text = "a" * 100
        assert self._truncate(text) == text

    def test_over_cap_truncated_with_ellipsis(self):
        """Text over the cap is truncated and ends with an ellipsis marker."""
        text = "a" * (self._max + 50)
        result = self._truncate(text)
        assert len(result) == self._max
        assert result.endswith("…")
        assert result.startswith("a")

    def test_exact_cap_passthrough(self):
        """Exactly-at-cap text is returned unchanged."""
        text = "b" * self._max
        assert self._truncate(text) == text
