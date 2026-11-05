"""/rate — the user's 1-5 verdict on a session plus optional free-text feedback.

Each rating is one JSON line appended to ``$HERMES_HOME/ratings.jsonl`` (append-only, so a
re-rating supersedes rather than overwrites; readers take the latest row per session). The
self-evolution pipeline joins these rows to ``state.db`` sessions by ``session_id`` and treats
low scores + comments as ground-truth signal about what the harness did well or badly.

Shared by every surface: CLI/TUI (``_handle_rate_command``) and the gateway.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

RATINGS_FILENAME = "ratings.jsonl"
MIN_RATING = 1
MAX_RATING = 5
MAX_COMMENT_CHARS = 4000
RATE_USAGE = (
    "Usage: /rate <1-5> [feedback]\n"
    "  e.g. /rate 4 great at the refactor, but ran tests too late"
)

# "<score>" optionally followed by "/5", then an optional separator (- : , .) and the comment.
_RATE_RE = re.compile(r"^\s*(\d+)(?:\s*/\s*5)?(?:\s*[-:,]\s*|\.\s+|\.$|\s+|$)(.*)$", re.DOTALL)


def ratings_path(home: Path | None = None) -> Path:
    """Location of the ratings log for *home* (default: the active Hermes home / profile)."""
    if home is None:
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
    return Path(home) / RATINGS_FILENAME


def _is_valid_score(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and MIN_RATING <= value <= MAX_RATING


def parse_rate_args(raw: str) -> tuple[int, str]:
    """``"4 nice work"`` → ``(4, "nice work")``. ``ValueError`` when the score is missing/invalid."""
    match = _RATE_RE.match(raw or "")
    if not match:
        raise ValueError(f"rating must start with a whole number {MIN_RATING}-{MAX_RATING}")
    score = int(match.group(1))
    if not _is_valid_score(score):
        raise ValueError(f"rating must be between {MIN_RATING} and {MAX_RATING}")
    return score, match.group(2).strip()


def record_rating(
    session_id: str,
    rating: int,
    comment: str,
    *,
    surface: str,
    model: str | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    """Append one rating row and return it. Raises ``ValueError`` on bad input, ``OSError`` on I/O."""
    if not session_id:
        raise ValueError("session_id is required")
    if not _is_valid_score(rating):
        raise ValueError(f"rating must be between {MIN_RATING} and {MAX_RATING}")
    row: dict[str, Any] = {
        "ts": time.time(),
        "session_id": str(session_id),
        "rating": rating,
        "comment": (comment or "").strip()[:MAX_COMMENT_CHARS],
        "surface": surface,
    }
    if model:
        row["model"] = str(model)
    path = ratings_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    # One O_APPEND write per row: concurrent writers (CLI + gateway) never interleave a line.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)
    return row


def load_ratings(home: Path | None = None) -> list[dict[str, Any]]:
    """All well-formed rating rows in file order; corrupt or invalid lines are skipped."""
    path = ratings_path(home)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    rows: list[dict[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("ratings: skipping corrupt line %d in %s", lineno, path)
            continue
        if isinstance(row, dict) and row.get("session_id") and _is_valid_score(row.get("rating")):
            rows.append(row)
    return rows


def latest_by_session(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Collapse re-ratings: the last row for each session wins."""
    return {row["session_id"]: row for row in rows}


def run_rate_command(
    args: str,
    *,
    session_id: str | None,
    surface: str,
    model: str | None = None,
    home: Path | None = None,
) -> str:
    """Parse, save and confirm a ``/rate`` invocation; returns the reply text for any surface."""
    if not (args or "").strip():
        return RATE_USAGE
    try:
        score, comment = parse_rate_args(args)
    except ValueError as exc:
        return f"⚠ {exc}\n{RATE_USAGE}"
    if not session_id:
        return "⚠ No active session to rate yet — send a message first."
    try:
        record_rating(session_id, score, comment, surface=surface, model=model, home=home)
    except OSError as exc:
        logger.error("ratings: failed to save rating for %s: %s", session_id, exc)
        return f"⚠ Could not save rating: {exc}"
    stars = "★" * score + "☆" * (MAX_RATING - score)
    note = " with feedback" if comment else ""
    return f"Saved rating {score}/5 {stars}{note} for session {session_id}. Thanks — this feeds self-evolution."
