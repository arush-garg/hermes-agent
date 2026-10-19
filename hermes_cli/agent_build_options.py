"""Config → AIAgent construction options shared by the classic CLI and the TUI/Desktop gateway.

``HermesCLI`` (``cli_init_mixin`` / ``cli_agent_setup_mixin``) and ``tui_gateway/server.py::_make_agent``
build the same agent; the two surfaces may differ only in presentation (callbacks, platform hint,
client-surface toolsets). Every functional knob they both read from ``config.yaml`` resolves here, from
the config dict the caller passes (``CLI_CONFIG`` for the CLI, the profile-scoped ``_load_cfg()`` for the
gateway), so one surface can no longer silently ignore a setting the other honors.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

_CHECKPOINT_DEFAULTS = {"max_snapshots": 20, "max_total_size_mb": 500, "max_file_size_mb": 10, "checkpoint_interval": 10}


def _section(cfg: Any, name: str) -> dict:
    value = cfg.get(name) if isinstance(cfg, dict) else None
    return value if isinstance(value, dict) else {}


def checkpoint_settings(cfg: Any, enabled_flag: bool = False) -> dict:
    """AIAgent checkpoint kwargs: ``--checkpoints`` (``enabled_flag``) OR ``checkpoints.enabled``, plus the
    configured limits and periodic interval. ``checkpoints: true`` (bare bool) is accepted."""
    raw = cfg.get("checkpoints") if isinstance(cfg, dict) else None
    section = {"enabled": raw} if isinstance(raw, bool) else (raw if isinstance(raw, dict) else {})
    get = lambda key: section.get(key, _CHECKPOINT_DEFAULTS[key])  # noqa: E731
    return {
        "checkpoints_enabled": bool(enabled_flag or section.get("enabled", False)),
        "checkpoint_max_snapshots": get("max_snapshots"),
        "checkpoint_max_total_size_mb": get("max_total_size_mb"),
        "checkpoint_max_file_size_mb": get("max_file_size_mb"),
        "checkpoint_interval": get("checkpoint_interval"),
    }


def openrouter_min_coding_score(cfg: Any) -> Optional[float]:
    """``openrouter.min_coding_score`` (Pareto Code router floor); out-of-range or unparseable = unset."""
    raw = _section(cfg, "openrouter").get("min_coding_score")
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if 0.0 <= value <= 1.0 else None


def service_tier(cfg: Any) -> Optional[str]:
    """Persisted ``agent.service_tier``: None (normal), ``"priority"``, ``"auto"`` or ``"cold"``."""
    from hermes_cli.cli_config_load import _parse_service_tier_config
    return _parse_service_tier_config(_section(cfg, "agent").get("service_tier", ""))


def fast_mode_request_overrides(tier: Optional[str], model: Any, provider: Any, base_url: Any) -> Optional[dict]:
    """Static fast mode (``priority``) is pinned into ``request_overrides`` at build time; ``auto``/``cold``
    are layered per request by ``agent.fast_mode``. None when the route has no fast override."""
    if tier != "priority":
        return None
    try:
        from hermes_cli.models import resolve_fast_mode_overrides
        return resolve_fast_mode_overrides(model, provider=provider or None, base_url=base_url or None)
    except Exception:
        logger.debug("fast-mode override resolution failed", exc_info=True)
        return None


def ephemeral_system_prompt(cfg: Any) -> str:
    """``HERMES_EPHEMERAL_SYSTEM_PROMPT`` wins, then the personality/``agent.system_prompt`` overlay."""
    from hermes_cli.personality import resolve_ephemeral_system_prompt
    return os.getenv("HERMES_EPHEMERAL_SYSTEM_PROMPT", "") or resolve_ephemeral_system_prompt(cfg)


def register_config_hooks(cfg: Any, *, accept_hooks: bool = False) -> None:
    """Shell hooks AND outbound webhooks from ``hooks:`` (both idempotent per profile home)."""
    from agent.outbound_webhooks import register_from_config as register_outbound_webhooks
    from agent.shell_hooks import register_from_config as register_shell_hooks
    register_shell_hooks(cfg, accept_hooks=accept_hooks)
    register_outbound_webhooks(cfg)


def persist_session_yolo(db: Any, session_key: str, enabled: bool) -> None:
    """Write the per-session YOLO flag onto the row so a resume on EITHER surface restores it. Best-effort:
    the in-memory toggle is authoritative; skipped without a store or for the ``default`` key."""
    if db is None or not session_key or session_key == "default":
        return
    try:
        db.set_session_yolo(session_key, bool(enabled))
    except Exception:
        logger.debug("persisting session yolo flag failed", exc_info=True)


def restore_persisted_session_yolo(session_meta: Any, session_key: str) -> bool:
    """Re-arm the per-session YOLO bypass a resumed row persisted (``model_config.yolo_mode``); the in-memory
    set starts empty in every process. True only when this call enabled it (never under a frozen
    process-level ``--yolo``, never on a malformed row)."""
    from hermes_state import SessionDB
    from tools.approval import _YOLO_MODE_FROZEN, enable_session_yolo, is_session_yolo_enabled
    if not session_key or _YOLO_MODE_FROZEN or not SessionDB.session_yolo_enabled(session_meta):
        return False
    if is_session_yolo_enabled(session_key):
        return False
    enable_session_yolo(session_key)
    return True


def resolve_auth_fallback_runtime(chain: Any, primary_exc: BaseException):
    """First complete ``provider``+``model`` fallback entry that resolves after the PRIMARY failed auth:
    ``(entry, runtime)`` or None (non-auth errors never fall back; provider-only entries are skipped so
    the unavailable primary model never leaks onto another runtime). A named ``providers:`` entry keeps
    its configured identity instead of the bare ``custom`` billing class (#98739)."""
    from hermes_cli.auth import AuthError, primary_failure_wording
    from hermes_cli.fallback_config import effective_runtime_provider, resolve_entry_api_key
    from hermes_cli.runtime_provider import resolve_runtime_provider
    if not isinstance(primary_exc, AuthError):
        return None
    for entry in chain if isinstance(chain, list) else []:
        if not isinstance(entry, dict):
            continue
        fb_provider, fb_model = (str(entry.get(k) or "").strip() for k in ("provider", "model"))
        fb_provider = fb_provider.lower()  # provider ids are lowercase slugs (CLI behavior)
        if not fb_provider or not fb_model:
            continue
        kwargs: dict = {"requested": fb_provider, "target_model": fb_model}
        if entry.get("base_url"):
            kwargs["explicit_base_url"] = entry["base_url"]
        if api_key := resolve_entry_api_key(entry):
            kwargs["explicit_api_key"] = api_key
        try:
            runtime = resolve_runtime_provider(**kwargs)
        except Exception:
            continue
        runtime["provider"] = effective_runtime_provider(entry, runtime)
        logger.warning("Primary provider %s (%s). Falling through to fallback: %s/%s",
                       primary_failure_wording(primary_exc)[0], primary_exc, fb_provider, fb_model)
        return {**entry, "provider": fb_provider, "model": fb_model}, runtime
    return None


@dataclass(frozen=True)
class StartupModelSelection:
    """Launch model choice. ``provider`` is "" when config's own provider decides; ``base_url`` /
    ``api_key`` are a URL-bearing alias's endpoint (explicit runtime-resolution inputs)."""
    model: str
    provider: str = ""
    base_url: str = ""
    api_key: str = ""


def resolve_startup_model_selection(cfg: Any, *, model: str = "", provider: str = "") -> StartupModelSelection:
    """Resolve the launch ``-m``/``--provider`` (else ``model.default``) the way both surfaces must:
    aliases / ``provider:model`` / configured ``provider/model`` (``resolve_startup_model_route``), an
    offline static-catalog provider guess for an explicit ``-m`` without ``--provider``, ``moa:<preset>``,
    and ``--provider <custom>`` without ``-m`` → that entry's default model (#86978)."""
    from hermes_cli.config import split_model_config_default
    model_cfg = cfg.get("model") if isinstance(cfg, dict) else None
    if isinstance(model_cfg, dict):
        cfg_model, nested_provider = split_model_config_default(model_cfg.get("default") or model_cfg.get("model") or "")
        cfg_provider = str(model_cfg.get("provider") or "").strip()
    else:
        cfg_model, nested_provider, cfg_provider = (str(model_cfg or "").strip(), "", "")
    cfg_provider = cfg_provider or os.getenv("HERMES_INFERENCE_PROVIDER", "").strip()
    model, provider = (model or "").strip(), (provider or "").strip()
    selected = model or cfg_model
    route_provider = base_url = api_key = ""
    if selected:
        from hermes_cli.model_switch import resolve_startup_model_route
        route = resolve_startup_model_route(
            selected, explicit_provider=provider, current_provider=provider or nested_provider or cfg_provider,
            user_providers=cfg.get("providers"), custom_providers=cfg.get("custom_providers"))
        if route is not None:
            selected, route_provider = route.model, route.provider
            base_url, api_key = route.base_url or "", route.api_key or ""
        elif model and not provider:
            from hermes_cli.models import detect_static_provider_for_model
            current = (nested_provider or cfg_provider or "auto").lower()
            if detected := detect_static_provider_for_model(model, current):
                route_provider, selected = detected
    moa_provider, selected = normalize_moa_model(selected)
    if not model and provider:
        try:
            from hermes_cli.runtime_provider import _get_named_custom_provider
            named = _get_named_custom_provider(provider)
        except Exception as exc:
            logger.warning("Could not resolve --provider %s default model; keeping global model.default (%s)",
                           provider, exc)
            named = None
        if default_model := str((named or {}).get("model") or "").strip():
            selected = default_model
    return StartupModelSelection(
        model=selected or "", provider=moa_provider or provider or route_provider, base_url=base_url, api_key=api_key)


def normalize_moa_model(model: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """``moa:<preset>`` -> ``("moa", preset)`` (same routing as ``/moa`` and the model picker); anything
    else -> ``(None, model)``. Without it the raw ``moa:<preset>`` string reaches the real provider and is
    rejected as an unsupported model (#56828). The prefix wins over ``--provider``."""
    if isinstance(model, str) and model.strip().lower().startswith("moa:"):
        preset = model.strip().split(":", 1)[1].strip()
        if preset:
            return "moa", preset
    return None, model
