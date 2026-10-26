"""Per-task ``cwd`` on delegate_task: the parent picks the directory a child starts in. The child's terminal,
relative file paths and project context files resolve from there; omitting it keeps the inherited cwd; bad
values fail the whole call before any child is built."""

import json
import os
import subprocess
import threading
from unittest.mock import MagicMock, patch

import pytest

from tools.delegate_tool import DELEGATE_TASK_SCHEMA, delegate_task
from tools.delegate_tool_tasks import _coerce_task_cwds


def _parent(task_id="parent-cwd-test"):
    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "sk-test"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "anthropic/claude-sonnet-4"
    parent.platform = "cli"
    parent._session_db = None
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    parent._current_task_id = task_id
    return parent


def _probe_child(seen):
    """A child whose turn runs the REAL terminal + file tools under its own task id."""
    from tools.file_tools import read_file_tool
    from tools.terminal_tool import terminal_tool

    child = MagicMock()

    def _run(user_message, task_id=None, **kw):
        seen["task_id"] = task_id
        seen["pwd"] = json.loads(terminal_tool("pwd -P", task_id=task_id))["output"].strip()
        seen["read"] = json.loads(read_file_tool("marker.txt", task_id=task_id))
        return {"final_response": "ok", "completed": True, "api_calls": 1}

    child.run_conversation.side_effect = _run
    return child


def _delegate(tasks, parent, child):
    with patch("run_agent.AIAgent", return_value=child) as MockAgent:
        out = json.loads(delegate_task(tasks=tasks, parent_agent=parent))
    return out, MockAgent


@pytest.fixture
def dirs(tmp_path):
    parent_dir = tmp_path / "parent"
    target = tmp_path / "target"
    for d, text in ((parent_dir, "PARENT"), (target, "TARGET")):
        d.mkdir()
        (d / "marker.txt").write_text(text)
    (target / "AGENTS.md").write_text("TARGET-REPO-CONVENTIONS-7731")
    return os.path.realpath(parent_dir), os.path.realpath(target)


@pytest.fixture
def seeded_parent(dirs):
    from tools.terminal_tool import clear_session_cwd, record_session_cwd
    parent = _parent()
    record_session_cwd(parent._current_task_id, dirs[0])
    yield parent
    clear_session_cwd(parent._current_task_id)


def test_schema_advertises_optional_per_task_cwd():
    item = DELEGATE_TASK_SCHEMA["parameters"]["properties"]["tasks"]["items"]
    assert item["properties"]["cwd"]["type"] == "string"
    assert "cwd" not in item["required"]
    assert "cwd" not in DELEGATE_TASK_SCHEMA["parameters"]["properties"]


def test_child_terminal_and_files_run_in_requested_cwd(dirs, seeded_parent):
    _, target = dirs
    seen = {}
    out, MockAgent = _delegate([{"goal": "Inspect the target repo", "cwd": target}], seeded_parent, _probe_child(seen))
    assert out["results"][0]["status"] == "completed", out
    assert seen["pwd"] == target
    assert "TARGET" in seen["read"]["content"]
    prompt = MockAgent.call_args.kwargs["ephemeral_system_prompt"]
    assert f"WORKSPACE PATH:\n{target}" in prompt
    assert "TARGET-REPO-CONVENTIONS-7731" in prompt  # AGENTS.md loaded from the requested cwd


def test_omitted_cwd_inherits_parent_cwd(dirs, seeded_parent):
    parent_dir, _ = dirs
    seen = {}
    out, MockAgent = _delegate([{"goal": "Inspect the parent repo"}], seeded_parent, _probe_child(seen))
    assert out["results"][0]["status"] == "completed", out
    assert seen["pwd"] == parent_dir
    assert "PARENT" in seen["read"]["content"]
    assert "TARGET-REPO-CONVENTIONS-7731" not in MockAgent.call_args.kwargs["ephemeral_system_prompt"]


def test_relative_and_tilde_cwd_resolve(dirs, seeded_parent, monkeypatch, tmp_path):
    parent_dir, target = dirs
    seen = {}
    out, _ = _delegate([{"goal": "Relative cwd", "cwd": "../target"}], seeded_parent, _probe_child(seen))
    assert out["results"][0]["status"] == "completed", out
    assert seen["pwd"] == target

    monkeypatch.setenv("HOME", str(tmp_path))
    seen = {}
    out, _ = _delegate([{"goal": "Tilde cwd", "cwd": "~/target"}], seeded_parent, _probe_child(seen))
    assert out["results"][0]["status"] == "completed", out
    assert seen["pwd"] == target


def test_child_cd_does_not_move_parent(dirs, seeded_parent):
    from tools.terminal_tool import get_session_cwd
    parent_dir, target = dirs
    seen = {}
    _delegate([{"goal": "Inspect the target repo", "cwd": target}], seeded_parent, _probe_child(seen))
    assert get_session_cwd(seeded_parent._current_task_id) == parent_dir


@pytest.mark.parametrize("use_cwd", [True, False])
def test_cwd_holds_under_bound_parent_session_key(dirs, use_cwd):
    """The CLI binds the approval session key (== parent task id) around the turn and the child thread inherits it
    via copy_context. The child's terminal must still read/write ITS OWN cwd record, never the parent's."""
    from tools.approval_context import reset_current_session_key, set_current_session_key
    from tools.terminal_tool import clear_session_cwd, get_session_cwd, record_session_cwd, terminal_tool
    parent_dir, target = dirs
    parent = _parent("cli-session-42")
    record_session_cwd(parent._current_task_id, parent_dir)
    seen = {}
    child = MagicMock()

    def _run(user_message, task_id=None, **kw):
        seen["pwd"] = json.loads(terminal_tool("pwd -P", task_id=task_id))["output"].strip()
        terminal_tool(f"cd {target}/..", task_id=task_id)
        seen["after_cd"] = json.loads(terminal_tool("pwd -P", task_id=task_id))["output"].strip()
        return {"final_response": "ok", "completed": True, "api_calls": 1}

    child.run_conversation.side_effect = _run
    token = set_current_session_key(parent._current_task_id)
    try:
        task = {"goal": "Probe the working directory", **({"cwd": target} if use_cwd else {})}
        out, _ = _delegate([task], parent, child)
    finally:
        reset_current_session_key(token)
        recorded_parent = get_session_cwd(parent._current_task_id)
        clear_session_cwd(parent._current_task_id)
    assert out["results"][0]["status"] == "completed", out
    assert seen["pwd"] == (target if use_cwd else parent_dir)
    assert seen["after_cd"] == os.path.dirname(target)  # the child's cd persists for the child...
    assert recorded_parent == parent_dir                 # ...and never moves the parent


def test_batch_children_get_independent_cwds(dirs, seeded_parent):
    parent_dir, target = dirs
    seen_a, seen_b = {}, {}
    children = iter([_probe_child(seen_a), _probe_child(seen_b)])
    with patch("run_agent.AIAgent", side_effect=lambda *a, **k: next(children)):
        out = json.loads(delegate_task(tasks=[
            {"goal": "Work in the target directory", "cwd": target},
            {"goal": "Work in the inherited parent directory"},
        ], parent_agent=seeded_parent))
    assert [r["status"] for r in out["results"]] == ["completed", "completed"], out
    assert seen_a["pwd"] == target and seen_b["pwd"] == parent_dir


@pytest.mark.parametrize("bad, needle", [
    ("does/not/exist", "not an existing directory"),
    (123, "must be a directory path string"),
])
def test_invalid_cwd_fails_before_any_child_is_built(seeded_parent, bad, needle):
    out, MockAgent = _delegate([{"goal": "Should never spawn", "cwd": bad}], seeded_parent, MagicMock())
    assert needle in out["error"]
    MockAgent.assert_not_called()


def test_file_as_cwd_is_rejected(dirs, seeded_parent):
    _, target = dirs
    out, MockAgent = _delegate(
        [{"goal": "Should never spawn", "cwd": os.path.join(target, "marker.txt")}], seeded_parent, MagicMock())
    assert "not an existing directory" in out["error"]
    MockAgent.assert_not_called()


def test_one_bad_cwd_in_batch_blocks_the_whole_batch(dirs, seeded_parent):
    _, target = dirs
    out, MockAgent = _delegate([
        {"goal": "Valid directory task", "cwd": target},
        {"goal": "Invalid directory task", "cwd": "/definitely/not/here"},
    ], seeded_parent, MagicMock())
    assert "Task 1" in out["error"]
    MockAgent.assert_not_called()


def test_coerce_task_cwds_shapes(tmp_path):
    base = str(tmp_path)
    (tmp_path / "sub").mkdir()
    cwds, err = _coerce_task_cwds([{"goal": "a"}, {"cwd": ""}, {"cwd": "  "}, {"cwd": "sub"}], base)
    assert err is None and cwds == [None, None, None, os.path.realpath(tmp_path / "sub")]
    # Relative without a base → explicit error, never silently the process cwd.
    _, err = _coerce_task_cwds([{"cwd": "sub"}], None)
    assert "relative" in err
    # Non-local backends: the host can't see the target filesystem, so only absolute-path normalisation applies.
    cwds, err = _coerce_task_cwds([{"cwd": "/workspace/app/../repo"}], None, local_backend=False)
    assert err is None and cwds == ["/workspace/repo"]


def test_worktree_isolation_branches_from_requested_cwd_repo(tmp_path, seeded_parent):
    repo = tmp_path / "other-repo"
    repo.mkdir()
    git = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)
    git("init", "-q")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    seen = {}
    with patch("tools.delegate_tool._get_worktree_isolation", return_value=True), \
            patch("tools.subagent_worktree.local_backend_active", return_value=True):
        out, _ = _delegate([{"goal": "Isolated work in other repo", "cwd": str(repo)}], seeded_parent, _probe_child(seen))
    assert out["results"][0]["status"] == "completed", out
    worktrees_root = os.path.realpath(repo / ".worktrees")
    assert seen["pwd"].startswith(worktrees_root + os.sep), seen["pwd"]
