"""Shared helpers for the per-profile MCP lifecycle RPCs (mcp.servers.*).

Published onto ``tui_gateway.server`` as ``_mcp_summarize_server`` so the rebound handler
bodies in methods_tools resolve it.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping


def server_configs_with_sources(config_servers: Mapping[str, dict]) -> tuple[Dict[str, dict], Dict[str, str | None]]:
    servers, origins = server_configs_with_origins(config_servers)
    return servers, {name: plugin for name, (_source, plugin) in origins.items()}


def server_configs_with_origins(
    config_servers: Mapping[str, dict],
) -> tuple[Dict[str, dict], Dict[str, tuple[str, str | None]]]:
    """``(servers, origins)`` for every server the runtime would connect: config.yaml, portable plugins,
    then the workspace ``.mcp.json`` (additive-only, same precedence as ``_load_mcp_config``). ``origins``
    maps name → ``(source, plugin)`` with source ``config`` / ``plugin`` / ``project``. Project servers live
    outside config.yaml, so they are listed but — like plugin servers — never written through it."""
    servers = {name: dict(cfg) for name, cfg in config_servers.items() if isinstance(cfg, dict)}
    origins: Dict[str, tuple[str, str | None]] = {name: ("config", None) for name in servers}
    try:
        from hermes_cli.plugins import discover_plugins, get_plugin_manager
        from tools.mcp_tool_config import _filter_suspicious_mcp_servers

        discover_plugins()
        manager = get_plugin_manager()
        portable = _filter_suspicious_mcp_servers(manager.get_portable_mcp_servers())
        owners = manager.get_portable_mcp_server_plugins()
        for name, cfg in portable.items():
            if name not in servers:
                servers[name] = dict(cfg)
                origins[name] = ("plugin", owners.get(name))
    except Exception:
        pass
    try:
        from tools.mcp_tool_config import _filter_suspicious_mcp_servers, _load_project_mcp_json

        for name, cfg in _filter_suspicious_mcp_servers(_load_project_mcp_json()).items():
            if name not in servers:
                servers[name] = dict(cfg)
                origins[name] = ("project", None)
    except Exception:
        pass
    return servers, origins


def server_write_error(name: str, origins: Mapping[str, tuple[str, str | None]]) -> str | None:
    """Reason a server cannot be edited through config.yaml, or None when it can."""
    source, plugin = origins.get(name, ("config", None))
    if source == "plugin":
        return f"server '{name}' is provided by plugin '{plugin}' and cannot be modified"
    if source == "project":
        return f"server '{name}' comes from the workspace .mcp.json; edit that file instead"
    return None


def summarize_server(name: str, cfg: dict, plugin: str | None = None, source: str | None = None) -> Dict[str, Any]:
    from hermes_cli.mcp_config import _oauth_tokens_present
    from tools.mcp_tool_common import mcp_server_enabled

    cfg = cfg if isinstance(cfg, dict) else {}
    transport = "http" if cfg.get("url") else ("stdio" if cfg.get("command") else "unknown")
    auth = cfg.get("auth")
    headers = cfg.get("headers") or {}
    if not auth and isinstance(headers, dict) and any(str(key).lower() == "authorization" for key in headers):
        auth = "header"
    return {
        "name": name,
        "transport": transport,
        "url": cfg.get("url"),
        "command": cfg.get("command"),
        "args": list(cfg.get("args") or []),
        "env": sorted(str(k) for k in (cfg.get("env") or {})),
        "auth": auth,
        "oauth_tokens_present": _oauth_tokens_present(name) if auth == "oauth" else None,
        "enabled": mcp_server_enabled(cfg),
        "tools": cfg.get("tools"),
        "source": source or ("plugin" if plugin is not None else "config"),
        "plugin": plugin}


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from typing import Optional  # noqa: F401,E402
from typing import Tuple  # noqa: F401,E402

def resolve_profile(rid, params, err_fn) -> Tuple[Optional[Any], Optional[dict]]:
    """Resolve the optional ``profile`` param to a HERMES_HOME override token.

    Returns ``(token, error)``: ``token`` is None for the launch profile (no
    override) or an opaque reset token; ``error`` is a JSON-RPC error dict
    (built via ``err_fn``) when the named profile doesn't exist. Callers reset
    ``token`` in a finally via :func:`reset_profile`.
    """
    profile = str(params.get("profile") or "").strip()
    if not profile:
        return None, None
    from hermes_cli.profiles import get_profile_dir
    from hermes_constants import set_hermes_home_override

    try:
        profile_dir = get_profile_dir(profile)
    except ValueError:
        return None, err_fn(rid, 4064, f"profile '{profile}' not found")
    if not profile_dir or not profile_dir.is_dir():
        return None, err_fn(rid, 4064, f"profile '{profile}' not found")
    return set_hermes_home_override(str(profile_dir)), None


def reset_profile(token) -> None:
    """Restore the profile override established by :func:`resolve_profile`."""
    if token is None:
        return
    try:
        from hermes_constants import reset_hermes_home_override

        reset_hermes_home_override(token)
    except Exception:
        pass
# ---- END PLUGIN-COMPAT ----
