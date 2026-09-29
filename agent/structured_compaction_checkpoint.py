"""Structured compaction checkpoint persistence/restoration.

Before (or with) compression, persist machine-readable task state to the session row so
resume/continued-context can reconstruct the agent's working set without turning a stale
prose summary into an active user instruction.

Design
------
The checkpoint lives in the ``checkpoint`` JSON text column of the sessions table.
It is separate from the compressed prose summary and the message history.
Structure is conservative: flat strings/lists are preferred over nested objects.

Schema
------
{
    "unresolved_targets": [<string>],
    "unresolved_tasks": [<string>],
    "terminal_outcomes": [<string>],
    "active_delegations": [<string>],
    "critical_user_constraints": [<string>],
    "authorized_side_effects": [<string>],
    "browser_ownership": null | { "origin": "<string>", "owned": <bool> },
    "pending_verification": [<string>]
}

Migration
---------
A new schema migration (v31) adds the column with no-op for existing rows.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class StructuredCompactionCheckpoint:
    """Machine-readable state persisted before/with compaction."""
    
    unresolved_targets: List[str] = field(default_factory=list)
    unresolved_tasks: List[str] = field(default_factory=list)
    terminal_outcomes: List[str] = field(default_factory=list)
    active_delegations: List[str] = field(default_factory=list)
    critical_user_constraints: List[str] = field(default_factory=list)
    authorized_side_effects: List[str] = field(default_factory=list)
    browser_ownership: Optional[Dict[str, Any]] = None
    pending_verification: List[str] = field(default_factory=list)
    
    def to_json(self) -> Optional[str]:
        """Serialize to JSON. Returns None if all fields are empty."""
        if self.is_empty():
            return None
        return json.dumps(asdict(self), ensure_ascii=False, separators=(',', ':'))
    
    @classmethod
    def from_json(cls, raw: Optional[str]) -> Optional[StructuredCompactionCheckpoint]:
        """Deserialize from JSON string."""
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return cls(**data)
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("Failed to parse checkpoint: %s", e)
            return None
    
    def is_empty(self) -> bool:
        """Check if checkpoint has no meaningful data."""
        return (
            not self.unresolved_targets
            and not self.unresolved_tasks
            and not self.terminal_outcomes
            and not self.active_delegations
            and not self.critical_user_constraints
            and not self.authorized_side_effects
            and not self.browser_ownership
            and not self.pending_verification
        )
    
    def merge(self, other: Optional[StructuredCompactionCheckpoint]) -> StructuredCompactionCheckpoint:
        """Merge another checkpoint into this one, preferring non-empty values."""
        if other is None or other.is_empty():
            return self
        if self.is_empty():
            return other
        
        def merge_list(existing: List[str], new: List[str]) -> List[str]:
            existing_set = set(existing)
            result = list(existing)
            for item in new:
                if item not in existing_set:
                    result.append(item)
                    existing_set.add(item)
            return result
        
        return StructuredCompactionCheckpoint(
            unresolved_targets=merge_list(self.unresolved_targets, other.unresolved_targets),
            unresolved_tasks=merge_list(self.unresolved_tasks, other.unresolved_tasks),
            terminal_outcomes=merge_list(self.terminal_outcomes, other.terminal_outcomes),
            active_delegations=merge_list(self.active_delegations, other.active_delegations),
            critical_user_constraints=merge_list(self.critical_user_constraints, other.critical_user_constraints),
            authorized_side_effects=merge_list(self.authorized_side_effects, other.authorized_side_effects),
            browser_ownership=other.browser_ownership or self.browser_ownership,
            pending_verification=merge_list(self.pending_verification, other.pending_verification),
        )
    
    def to_prose_summary(self) -> str:
        """Convert to a human-readable prose summary for inclusion in context."""
        parts = []
        if self.unresolved_targets:
            parts.append("Unresolved targets: " + "; ".join(self.unresolved_targets))
        if self.unresolved_tasks:
            parts.append("Unresolved tasks: " + "; ".join(self.unresolved_tasks))
        if self.terminal_outcomes:
            parts.append("Terminal outcomes: " + "; ".join(self.terminal_outcomes))
        if self.active_delegations:
            parts.append("Active delegations: " + "; ".join(self.active_delegations))
        if self.critical_user_constraints:
            parts.append("Critical user constraints: " + "; ".join(self.critical_user_constraints))
        if self.authorized_side_effects:
            parts.append("Authorized side effects: " + "; ".join(self.authorized_side_effects))
        if self.browser_ownership:
            parts.append(f"Browser ownership: {json.dumps(self.browser_ownership)}")
        if self.pending_verification:
            parts.append("Pending verification: " + "; ".join(self.pending_verification))
        return "\n".join(parts)


def build_checkpoint_from_messages(
    messages: List[Dict[str, Any]],
    *,
    max_items: int = 20,
) -> StructuredCompactionCheckpoint:
    """Best-effort extraction from recent messages.
    
    Looks for explicit markers like [UNRESOLVED], [DELEGATED], etc.
    This is conservative - prefer explicit set_checkpoint() calls over parsing.
    """
    checkpoint = StructuredCompactionCheckpoint()
    
    for msg in messages[-max_items:]:
        content = msg.get("content", "")
        if not content or not isinstance(content, str):
            continue
        
        # Parse structured markers
        for line in content.split("\n"):
            line = line.strip()
            if not line:
                continue
            
            # Check for various markers
            for prefix, field_name in [
                ("[UNRESOLVED_TARGET]", "unresolved_targets"),
                ("[UNRESOLVED_TASK]", "unresolved_tasks"),
                ("[TERMINAL_OUTCOME]", "terminal_outcomes"),
                ("[DELEGATED]", "active_delegations"),
                ("[CONSTRAINT]", "critical_user_constraints"),
                ("[SIDE_EFFECT]", "authorized_side_effects"),
                ("[VERIFICATION]", "pending_verification"),
            ]:
                if line.startswith(prefix):
                    item = line[len(prefix):].strip()
                    if item:
                        getattr(checkpoint, field_name).append(item)
    
    return checkpoint
