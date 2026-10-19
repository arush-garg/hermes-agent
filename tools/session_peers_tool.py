"""sessions_peers: list live Hermes sessions on this machine and send one a short message.

Transport is tools/peer_inbox.py; the receiving session injects the message like a /steer,
framed with a PEER marker (no user authority).
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from tools import peer_inbox
from tools.registry import registry

log = logging.getLogger(__name__)

# Patchable so tests don't wait: how long ``send`` polls for a delivery receipt.
SEND_WAIT_SECONDS = 3.0
SEND_POLL_INTERVAL = 0.1
SHORT_ID_LEN = 6
MAX_MESSAGE_CHARS = 8000


def _home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def _own_session_id(kw: dict) -> str:
    return str(kw.get("session_id") or os.environ.get("HERMES_SESSION_ID", "") or "")


def _session_rows(session_ids: list[str]) -> dict[str, dict[str, Any]]:
    """state.db rows for ``session_ids`` (read-only; missing db → {})."""
    db_path = _home() / "state.db"
    if not session_ids or not db_path.is_file():
        return {}
    from hermes_state import SessionDB
    rows: dict[str, dict[str, Any]] = {}
    try:
        db = SessionDB(db_path=db_path, read_only=True)
    except Exception as exc:
        log.debug("sessions_peers: state.db unavailable: %s", exc)
        return {}
    try:
        for sid in session_ids:
            try:
                row = db.get_session(sid)
            except Exception:
                row = None
            if row:
                rows[sid] = dict(row)
    finally:
        db.close()
    return rows


def _title_of(row: dict[str, Any] | None, presence: dict[str, Any] | None) -> str:
    row = row or {}
    return str(row.get("title") or row.get("display_name") or (presence or {}).get("title") or "")


def _live_peers(self_id: str) -> list[dict[str, Any]]:
    from hermes_cli.active_sessions import active_session_registry_snapshot
    try:
        entries = active_session_registry_snapshot()
    except Exception as exc:
        log.debug("sessions_peers: registry unavailable: %s", exc)
        entries = []
    seen: dict[str, dict[str, Any]] = {}
    for entry in entries:
        sid = str(entry.get("session_id") or "")
        if sid and sid != self_id and sid not in seen:
            seen[sid] = entry
    # A live receiver can publish presence before it holds a registry lease (TUI sessions lease lazily on
    # the first prompt), so presence-only sessions are peers too.
    for sid in peer_inbox.live_presence_ids():
        if sid != self_id and sid not in seen:
            seen[sid] = {"session_id": sid}
    rows = _session_rows(list(seen))
    peers = []
    for sid, entry in seen.items():
        row = rows.get(sid) or {}
        presence = peer_inbox.read_presence(sid)
        peers.append({
            "id": sid,
            "short": sid[-SHORT_ID_LEN:],
            "surface": str(entry.get("surface") or (presence or {}).get("surface") or ""),
            "title": _title_of(row, presence),
            "cwd": str(row.get("cwd") or (presence or {}).get("cwd") or ""),
            "busy": bool(presence.get("busy")) if presence else None,
            "can_receive": bool(presence) and presence.get("inbound") != "refuse",
            "inbound": (presence or {}).get("inbound") or "unavailable",
            "last_activity": str(row.get("last_activity_description") or ""),
        })
    return peers


def _resolve(to: str, peers: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]]]:
    """(session_id, []) on a unique match, else (None, candidates)."""
    for peer in peers:
        if peer["id"] == to:
            return to, []
    if peer_inbox.read_presence(to) is not None:  # live receiver not (yet) in the lease registry
        return to, []
    by_suffix = [p for p in peers if p["id"].endswith(to)]
    if len(by_suffix) == 1:
        return by_suffix[0]["id"], []
    if len(by_suffix) > 1:
        return None, by_suffix
    by_title = [p for p in peers if p["title"] and p["title"] == to]
    if not by_title:
        by_title = [p for p in peers if p["title"] and p["title"].casefold() == to.casefold()]
    if len(by_title) == 1:
        return by_title[0]["id"], []
    return None, by_title


def _brief(peer: dict[str, Any]) -> dict[str, str]:
    return {"id": peer["id"], "short": peer["short"], "title": peer["title"], "cwd": peer["cwd"]}


def _send(args: dict, self_id: str) -> dict[str, Any]:
    to = str(args.get("to") or "").strip()
    message = args.get("message")
    message = message.strip() if isinstance(message, str) else ""
    if not to:
        return {"status": "refused", "message_id": None, "to": "", "detail": "'to' is required"}
    if not message:
        return {"status": "refused", "message_id": None, "to": to, "detail": "'message' is required"}
    if len(message) > MAX_MESSAGE_CHARS:
        return {"status": "refused", "message_id": None, "to": to,
                "detail": f"message too long ({len(message)} > {MAX_MESSAGE_CHARS} chars); pass file paths instead"}
    peers = _live_peers(self_id)
    target, candidates = _resolve(to, peers)
    if target is None:
        detail = (f"'{to}' is ambiguous; use a full session id" if candidates
                  else f"no live session matches '{to}'; call sessions_peers(action=\"list\")")
        out: dict[str, Any] = {"status": "refused", "message_id": None, "to": to, "detail": detail}
        if candidates:
            out["candidates"] = [_brief(p) for p in candidates]
        return out
    own_row = _session_rows([self_id]).get(self_id)
    posted = peer_inbox.post(
        target, from_session_id=self_id, from_title=_title_of(own_row, None),
        from_cwd=str((own_row or {}).get("cwd") or os.getcwd()), body=message)
    if posted.get("status") != "queued":
        return {"status": "refused", "message_id": None, "to": target, "detail": posted.get("reason", "refused")}
    message_id = posted["message_id"]
    deadline = time.monotonic() + max(0.0, float(SEND_WAIT_SECONDS))
    while True:
        receipt = peer_inbox.get_receipt(target, message_id)
        if receipt is not None:
            outcome = receipt.get("outcome")
            if outcome == "failed":
                return {"status": "refused", "message_id": message_id, "to": target,
                        "detail": f"receiver failed to deliver: {receipt.get('detail') or 'unknown error'}"}
            how = "mid-turn (like /steer)" if outcome == "delivered_steer" else "as its next turn"
            return {"status": "delivered", "message_id": message_id, "to": target,
                    "detail": f"delivered {how}"}
        if time.monotonic() >= deadline:
            break
        time.sleep(SEND_POLL_INTERVAL)
    return {"status": "queued", "message_id": message_id, "to": target,
            "detail": "queued; the receiving session will pick it up on its next poll"}


def handle_sessions_peers(args: dict, **kw) -> str:
    self_id = _own_session_id(kw)
    action = str((args or {}).get("action") or "").strip().lower()
    if action == "list":
        return json.dumps({"self": self_id, "sessions": _live_peers(self_id)}, ensure_ascii=False)
    if action == "send":
        if not self_id:
            return json.dumps({"status": "refused", "message_id": None, "to": str(args.get("to") or ""),
                               "detail": "no session id available for this session"})
        return json.dumps(_send(args, self_id), ensure_ascii=False)
    return json.dumps({"error": "action must be 'list' or 'send'"})


SESSIONS_PEERS_SCHEMA = {
    "name": "sessions_peers",
    "description": (
        "List the other live Hermes sessions on this machine, or send one a short text message by id. "
        "The message arrives in that session like a /steer (mid-turn if it is busy, otherwise as its next "
        "turn), clearly marked as coming from this session — not from the user. Use it to hand over findings "
        "or ask a peer a question. Don't spam: one concise message beats many. No file transfer — pass file "
        "paths instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "send"],
                       "description": "'list' live peer sessions, or 'send' a message to one."},
            "to": {"type": "string",
                   "description": "send: target session id, its short id (last 6 chars), or its exact unique title."},
            "message": {"type": "string", "description": "send: the text to deliver (concise)."},
        },
        "required": ["action"],
    },
}


registry.register(
    name="sessions_peers",
    toolset="sessions_peers",
    schema=SESSIONS_PEERS_SCHEMA,
    handler=handle_sessions_peers,
    emoji="📡",
)
