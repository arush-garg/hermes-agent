"""``${VAR}`` rendering in MCP config strings: shell defaults and the resolution record."""

from __future__ import annotations

from tools import mcp_tool_config as cfg


def test_inline_default_is_used_when_the_variable_is_unset(monkeypatch):
    monkeypatch.delenv("HERMES_TEST_MCP_UNSET", raising=False)

    out = cfg._interpolate_env_vars("${HERMES_TEST_MCP_UNSET:-fallback}")

    assert out == "fallback"
    assert out.missed is False


def test_unset_reference_keeps_its_placeholder_and_is_recorded(monkeypatch):
    monkeypatch.delenv("HERMES_TEST_MCP_UNSET", raising=False)

    out = cfg._interpolate_env_vars("${HERMES_TEST_MCP_UNSET}")

    assert out == "${HERMES_TEST_MCP_UNSET}"
    assert out.missed is True and out.unset_ref is True


def test_a_resolved_value_is_final_and_a_missed_one_rerenders_from_its_template(monkeypatch):
    monkeypatch.delenv("HERMES_TEST_MCP_KEY", raising=False)
    first = cfg._interpolate_env_vars("Bearer ${HERMES_TEST_MCP_KEY}")
    monkeypatch.setenv("HERMES_TEST_MCP_KEY", "s3cret")

    assert cfg._interpolate_env_vars(first) == "Bearer s3cret"


def test_plain_strings_pass_through_untouched():
    assert cfg._interpolate_env_vars("no placeholders") == "no placeholders"
