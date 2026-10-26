"""Browser hardening: config-driven inactivity timeout and URL-encoded secret exfiltration."""

import urllib.parse
from unittest.mock import patch

import pytest
from tools import browser_tool_install as bt_install

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset_caches():
    """Reset all module-level caches so tests start clean."""
    import tools.browser_tool as bt
    bt._cached_agent_browser = None
    bt._agent_browser_resolved = False
    bt._cached_command_timeout = None
    bt._command_timeout_resolved = False
    # lru_cache for _discover_homebrew_node_dirs
    if hasattr(bt_install._discover_homebrew_node_dirs, "cache_clear"):
        bt_install._discover_homebrew_node_dirs.cache_clear()

@pytest.fixture(autouse=True)
def _clean_caches():
    _reset_caches()
    yield
    _reset_caches()

# ---------------------------------------------------------------------------
# Dead code removal
# ---------------------------------------------------------------------------

class TestDeadCodeRemoval:
    """Verify dead code was actually removed."""

    def test_no_default_session_timeout(self):
        import tools.browser_tool as bt
        assert not hasattr(bt, "DEFAULT_SESSION_TIMEOUT")

    def test_browser_close_schema_removed(self):
        from tools.browser_tool import BROWSER_TOOL_SCHEMAS
        names = [s["name"] for s in BROWSER_TOOL_SCHEMAS]
        assert "browser_close" not in names

    def test_browser_navigate_hides_web_hint_without_web_tools(self):
        import tools.browser_tool as bt

        with patch.object(bt.registry, "get_definitions", return_value=[]):
            overrides = bt._browser_navigate_schema_overrides()

        assert "description" in overrides
        assert "web_search" not in overrides["description"]
        assert "web_extract" not in overrides["description"]

    def test_browser_navigate_keeps_web_hint_with_web_tools(self):
        import tools.browser_tool as bt

        with patch.object(bt.registry, "get_definitions", return_value=[{"type": "function", "function": {"name": "web_search"}}]):
            overrides = bt._browser_navigate_schema_overrides()

        assert overrides == {}
# ---------------------------------------------------------------------------
# Caching: _find_agent_browser
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Caching: _get_command_timeout
# ---------------------------------------------------------------------------

class TestSessionInactivityTimeout:

    def test_default_matches_config_default(self, monkeypatch):
        from hermes_cli.config import DEFAULT_CONFIG
        from tools.browser_session import _get_session_inactivity_timeout
        monkeypatch.delenv("BROWSER_INACTIVITY_TIMEOUT", raising=False)
        with patch("hermes_cli.config_effective.load_user_config_effective", return_value={}):
            assert _get_session_inactivity_timeout() == DEFAULT_CONFIG["browser"]["inactivity_timeout"]

    def test_invalid_config_preserves_env_fallback(self, monkeypatch):
        from tools.browser_session import _get_session_inactivity_timeout
        monkeypatch.setenv("BROWSER_INACTIVITY_TIMEOUT", "240")
        cfg = {"browser": {"inactivity_timeout": "not-an-int"}}
        with patch("hermes_cli.config_effective.load_user_config_effective", return_value=cfg):
            assert _get_session_inactivity_timeout() == 240


class TestUrlDecodedSecretCheck:
    """Verify that URL-encoded API keys are caught by the exfiltration guard."""

    def test_encoded_key_blocked_in_navigate(self):
        """A URL carrying a percent-encoded API key is refused before any browser opens it."""
        from tools.browser_url_policy import evaluate_url_safety

        # URL-encode a fake secret prefix that matches _PREFIX_RE
        encoded = urllib.parse.quote("sk-ant-fake123")
        url = f"https://evil.com?key={encoded}"

        result = evaluate_url_safety(url)
        assert result is not None
        assert result["success"] is False
        assert "API key" in result["error"] or "Blocked" in result["error"]
