"""Opt-in subscription-first policy, shared by native Anthropic credential paths."""
from __future__ import annotations


def oauth_first_enabled() -> bool:
    # Use the pool's profile-aware, frozen-runtime config reader, not process env.
    from agent.credential_pool import _load_config_safe
    config = _load_config_safe() or {}
    auth = config.get("auth") or {}
    return isinstance(auth, dict) and auth.get("anthropic_oauth_first") is True


def prefer_oauth_entries(provider, entries):
    """Keep selection strategies within the OAuth tier until it is unavailable."""
    if provider == "anthropic" and oauth_first_enabled():
        oauth = [entry for entry in entries if entry.auth_type == "oauth"]
        if oauth:
            return oauth
    return entries


def resolve_preferred_token(model=None):
    """Read the same model-gated ladder without leasing, refreshing or clearing status."""
    from agent.credential_pool import load_pool
    pool = load_pool("anthropic")
    with pool._lock:
        entries, _ = pool._available_entries(model=model)
        entries = prefer_oauth_entries("anthropic", entries)
        return entries[0].runtime_api_key if entries else None
