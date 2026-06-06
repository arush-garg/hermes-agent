"""Claude Code CLI provider profile.

claude-cli runs Claude turns through the locally-installed `claude` binary
(first-party → draws from the Pro/Max plan, not "extra usage") instead of
calling the Anthropic API directly. Like copilot-acp, it is an external
subprocess routed through the chat_completions path; the actual client is
``agent.claude_cli_client.ClaudeCLIClient``, selected in
``agent_runtime_helpers.create_openai_client``. Hermes' full tool surface is
exposed to the subprocess via an in-process MCP bridge.
"""

from providers import register_provider
from providers.base import ProviderProfile


class ClaudeCLIProfile(ProviderProfile):
    """Claude Code CLI — external process; models come from the CLI itself."""

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        # Common Claude aliases the `claude --model` flag accepts, plus pinned
        # IDs. The CLI resolves aliases to the current model.
        return [
            "opus",
            "sonnet",
            "haiku",
            "claude-opus-4-8",
            "claude-sonnet-4-6",
            "claude-haiku-4-5-20251001",
        ]


claude_cli = ClaudeCLIProfile(
    name="claude-cli",
    aliases=("claude-code", "claude-sub", "claude-oauth-cli"),
    api_mode="chat_completions",  # external subprocess uses chat_completions routing
    env_vars=("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_TOKEN"),
    base_url="claude-cli://local",  # internal scheme; selects ClaudeCLIClient
    auth_type="external_process",
    display_name="Claude Code (CLI / subscription)",
    description="Runs Claude via the local `claude` binary so usage draws from your Pro/Max plan",
)

register_provider(claude_cli)
