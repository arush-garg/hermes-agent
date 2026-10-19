"""Surface-neutral bodies for slash commands that read or act on the LIVE agent.

The classic CLI owns the agent in-process; the TUI's slash worker is a separate HermesCLI that
resumes the session WITHOUT building an agent, so any handler reading ``self.agent`` /
``conversation_history`` there always answers "send a message first". These helpers take the
agent + history explicitly so both surfaces run one implementation: the CLI passes its own,
the TUI gateway passes the live session's (``tui_gateway/methods_slash.py``).
"""

from __future__ import annotations

NO_AGENT_CONTEXT = "(._.) No active agent -- send a message first."
NOTHING_TO_REFINE = "Nothing to refine yet — send a message first."
EMPTY_REFINE = "Nothing to refine yet — the conversation is empty."


def context_breakdown_lines(agent, history, arg: str = "", model: str = "") -> list[str]:
    """`/context [all]` body: header, 5×20 glyph grid + per-category table, context-file sources.

    Read-only (chars/4 estimate, no provider call). Raises on compute failure so callers can
    word the error for their surface. ``all``/``full``/``details`` appends per-skill/toolset costs.
    """
    from agent.context_breakdown import (
        compute_context_details, compute_session_context_breakdown, render_context_breakdown_lines)
    from agent.context_file_sources import context_file_sources_for_agent, render_context_file_lines

    payload = compute_session_context_breakdown(agent, list(history or []))
    details = None
    if (arg or "").strip().lower() in {"all", "full", "details"}:
        try:
            details = compute_context_details(agent)
        except Exception:
            details = {"skills": [], "toolsets": []}
    try:
        file_lines = render_context_file_lines(context_file_sources_for_agent(agent))
    except Exception:
        file_lines = []
    header = f"🧠 Context Usage — {payload.get('model') or model or getattr(agent, 'model', '')}"
    body = render_context_breakdown_lines(payload, details=details, grid=True)
    return [header, "", *body, *([""] + file_lines if file_lines else [])]


def start_refine(agent, history, focus: str = "") -> tuple[bool, str]:
    """`/refine [focus]` body: spawn the memory/skill review fork on demand (same machinery as the
    automatic post-turn review). Returns ``(started, message)``; the live conversation and prompt
    cache are never touched."""
    if agent is None:
        return False, NOTHING_TO_REFINE
    snapshot = list(history or [])
    if not snapshot:
        return False, EMPTY_REFINE
    try:
        agent._spawn_background_review(
            messages_snapshot=snapshot, review_memory=True,
            review_skills="skill_manage" in getattr(agent, "valid_tool_names", set()),
            focus=focus or None, explicit=True)
    except Exception as exc:
        return False, f"/refine failed to start: {exc}"
    tail = f" (focus: {focus})" if focus else ""
    return True, (f"⚗ Reviewing this conversation in the background{tail} — "
                  f"any memory/skill updates will be reported when done.")
