"""Test structured compaction checkpoint persistence/restoration."""
from __future__ import annotations

import json

from agent.structured_compaction_checkpoint import StructuredCompactionCheckpoint


def test_checkpoint_serialization():
    """Test StructuredCompactionCheckpoint to/from JSON."""
    cp = StructuredCompactionCheckpoint(
        unresolved_targets=["target1", "target2"],
        unresolved_tasks=["task1"],
        terminal_outcomes=["outcome1"],
        active_delegations=["delegate1"],
        critical_user_constraints=["constraint1"],
        authorized_side_effects=["effect1"],
        browser_ownership={"origin": "https://example.com", "owned": True},
        pending_verification=["verify1"],
    )
    # Serialize
    json_str = cp.to_json()
    assert json_str is not None
    data = json.loads(json_str)
    assert data["unresolved_targets"] == ["target1", "target2"]
    assert data["browser_ownership"]["origin"] == "https://example.com"
    # Deserialize
    cp2 = StructuredCompactionCheckpoint.from_json(json_str)
    assert cp2 == cp
    # Empty checkpoint returns None
    empty = StructuredCompactionCheckpoint()
    assert empty.to_json() is None
    assert StructuredCompactionCheckpoint.from_json(None) is None
    assert StructuredCompactionCheckpoint.from_json("") is None


def test_checkpoint_merge():
    """Test merging checkpoints."""
    cp1 = StructuredCompactionCheckpoint(
        unresolved_targets=["a", "b"],
        active_delegations=["x"],
    )
    cp2 = StructuredCompactionCheckpoint(
        unresolved_targets=["b", "c"],
        active_delegations=["y"],
        terminal_outcomes=["out"],
    )
    merged = cp1.merge(cp2)
    # Order preserved, no duplicates
    assert set(merged.unresolved_targets) == {"a", "b", "c"}
    assert set(merged.active_delegations) == {"x", "y"}
    assert merged.terminal_outcomes == ["out"]
    # Merging with empty returns self
    assert cp1.merge(None) is cp1
    assert cp1.merge(StructuredCompactionCheckpoint()) is cp1
    # Merging empty with cp returns cp
    assert StructuredCompactionCheckpoint().merge(cp2) == cp2
