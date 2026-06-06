"""Native Anthropic provider profile."""

import json
import logging
import urllib.request

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)


class AnthropicProfile(ProviderProfile):
    """Native Anthropic — uses x-api-key header, not Bearer."""

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """List models for the active credential.

        Regular Console API keys (``sk-ant-api*``) authenticate with the
        ``x-api-key`` header. OAuth / Claude Code subscription tokens
        (``sk-ant-oat*``, ``cc-*``, JWTs) are rejected by ``x-api-key`` with
        ``401 invalid x-api-key`` — they must use ``Authorization: Bearer``
        plus the OAuth beta header. Without this branch, selecting the
        ``anthropic`` provider with an OAuth token fails to list any models.
        """
        if not api_key:
            return None
        try:
            from agent.anthropic_adapter import _is_oauth_token
            is_oauth = _is_oauth_token(api_key)
        except Exception:
            is_oauth = api_key.startswith(("sk-ant-oat", "cc-", "eyJ"))
        try:
            req = urllib.request.Request("https://api.anthropic.com/v1/models")
            if is_oauth:
                req.add_header("Authorization", f"Bearer {api_key}")
                req.add_header("anthropic-beta", "oauth-2025-04-20")
            else:
                req.add_header("x-api-key", api_key)
            req.add_header("anthropic-version", "2023-06-01")
            req.add_header("Accept", "application/json")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            return [
                m["id"]
                for m in data.get("data", [])
                if isinstance(m, dict) and "id" in m
            ]
        except Exception as exc:
            logger.debug("fetch_models(anthropic): %s", exc)
            return None


anthropic = AnthropicProfile(
    name="anthropic",
    aliases=("claude", "claude-oauth", "claude-code"),
    api_mode="anthropic_messages",
    env_vars=("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"),
    base_url="https://api.anthropic.com",
    auth_type="api_key",
    default_aux_model="claude-haiku-4-5-20251001",
)

register_provider(anthropic)
