"""Rate-limit retry cadence for same-provider credential recovery."""


def recover_rate_limit(pool, *, has_retried_429, error_context, api_key_hint, credential_id, rotate_and_swap):
    from agent.agent_runtime_helpers import _ra, _USAGE_LIMIT_REASON_TOKENS, _USAGE_LIMIT_MESSAGE_TOKENS
    from agent.credential_pool import STATUS_EXHAUSTED

    # Already-exhausted credential: rotate immediately, including on a new prompt.
    current_entry = None
    if credential_id:
        current_entry = next((e for e in pool.entries() if e.id == credential_id), None)
    if api_key_hint:
        current_entry = current_entry or next(
            (e for e in pool.entries() if e.runtime_api_key == api_key_hint), None
        )
    if current_entry is None:
        current_entry = pool.current()
    current_last_status = getattr(current_entry, "last_status", None) if current_entry else None
    if current_last_status == STATUS_EXHAUSTED:
        _ra().logger.info(
            "Credential already exhausted (last_status=%s) — rotating immediately instead of retrying",
            current_last_status,
        )
        return (True, False) if rotate_and_swap(429, "rate limit, pre-exhausted") else (False, True)
    usage_limit_reached = False
    if error_context:
        context_reason = str(error_context.get("reason") or "").lower()
        context_message = str(error_context.get("message") or "").lower()
        usage_limit_reached = any(t in context_reason for t in _USAGE_LIMIT_REASON_TOKENS) or any(
            t in context_message for t in _USAGE_LIMIT_MESSAGE_TOKENS
        )
    if not has_retried_429 and not usage_limit_reached:
        return False, True
    return (True, False) if rotate_and_swap(429, "rate limit") else (False, True)
