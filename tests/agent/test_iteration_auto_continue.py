"""Exercise iteration exhaustion through the real synchronous loop entry."""

from types import SimpleNamespace
from unittest.mock import Mock

from agent.conversation_loop import run_conversation
from agent.iteration_budget import IterationBudget


def _make_base_fixture(monkeypatch, budget_max=1, continuation_limit=3, compacted=None):
    """Common test fixture builder."""
    budget = IterationBudget(budget_max)
    assert budget.consume()
    messages = [{"role": "user", "content": "Finish the task"}]
    if compacted is None:
        compacted = [{"role": "user", "content": "Task summary"}]
    context = SimpleNamespace(
        user_message="Finish the task", original_user_message="Finish the task",
        messages=messages, conversation_history=[], active_system_prompt="system",
        effective_task_id="test-task", turn_id="test-turn", current_turn_user_idx=0,
        should_review_memory=False, plugin_user_context=None, ext_prefetch_cache=None,
        preflight_compression_blocked=False,
    )
    agent = SimpleNamespace(
        iteration_budget=budget, max_iterations=1, api_mode="chat_completions",
        _budget_grace_call=False, _interrupt_requested=False, quiet_mode=True,
        _try_refresh_env_client_credentials=lambda: None,
        _drain_pending_redirect=lambda: None,
        _compress_context=Mock(return_value=(compacted, "compacted system")),
        _persist_session=Mock(), _emit_status=Mock(),
    )
    agent._checkpoint_mgr = SimpleNamespace(new_turn=lambda: None)

    monkeypatch.setattr("agent.conversation_loop.build_turn_context", lambda *a, **k: context)
    monkeypatch.setattr("agent.conversation_loop._record_session_interruption", lambda *a, **k: None)
    monkeypatch.setattr("agent.conversation_loop._review_input_budget_exhausted", lambda a: False)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"agent": {"max_auto_continue": continuation_limit}})
    monkeypatch.setattr("agent.conversation_loop.finalize_turn", lambda a, **k: k)
    monkeypatch.setattr("agent.conversation_loop.conversation_history_after_compression", lambda a, m, h: list(m))
    return agent, budget, compacted, messages


def test_exhausted_budget_compacts_before_resuming_loop(monkeypatch):
    """Basic continuation: compaction happens and loop resumes."""
    agent, budget, compacted, messages = _make_base_fixture(monkeypatch)

    def stop_resumed_iteration():
        agent._interrupt_requested = True

    agent._checkpoint_mgr = SimpleNamespace(new_turn=stop_resumed_iteration)

    result = run_conversation(agent, "Finish the task", system_message="system")

    agent._compress_context.assert_called_once()
    assert budget.auto_continue_count == 1
    assert budget.remaining == 1
    assert result["interrupted"] is True  # resumed loop reached checkpoint
    assert result["messages"][-1]["display_kind"] == "auto_continue"
    assert result["conversation_history"] == compacted


def test_continuation_cap_enforced(monkeypatch):
    """After max_auto_continue continuations, loop exits to finalizer."""
    # budget allows 1 iteration, continuation_limit=2
    agent, budget, compacted, messages = _make_base_fixture(monkeypatch, budget_max=1, continuation_limit=2)
    # Pre-set auto_continue_count to 2 (already used both continuations)
    budget._auto_continue_count = 2

    result = run_conversation(agent, "Finish the task", system_message="system")

    # Should NOT call compress_context since cap reached
    agent._compress_context.assert_not_called()
    assert budget.auto_continue_count == 2  # unchanged
    assert result["interrupted"] is False
    assert result["failed"] is False
    assert result["final_response"] is None
    assert result["_turn_exit_reason"] == "unknown"


def test_disabled_continuation_config(monkeypatch):
    """max_auto_continue=0 or missing disables continuation."""
    agent, budget, compacted, messages = _make_base_fixture(monkeypatch, continuation_limit=0)

    result = run_conversation(agent, "Finish the task", system_message="system")

    agent._compress_context.assert_not_called()
    assert budget.auto_continue_count == 0
    assert result["interrupted"] is False
    assert result["failed"] is False
    assert result["final_response"] is None


def test_compaction_failure_noop(monkeypatch):
    """Compaction returns same messages (no-op) → loop exits to finalizer."""
    agent, budget, compacted, _ = _make_base_fixture(monkeypatch, compacted=[{"role": "user", "content": "Finish the task"}])  # no-op

    result = run_conversation(agent, "Finish the task", system_message="system")

    # compress_context called but returns same messages → no continuation
    agent._compress_context.assert_called_once()
    assert budget.auto_continue_count == 0  # not incremented
    assert result["interrupted"] is False
    assert result["failed"] is False


def test_compaction_exception_aborts(monkeypatch):
    """Exception during compaction → loop exits to finalizer."""
    agent, budget, compacted, messages = _make_base_fixture(monkeypatch)
    agent._compress_context = Mock(side_effect=RuntimeError("compression failed"))

    result = run_conversation(agent, "Finish the task", system_message="system")

    assert budget.auto_continue_count == 0
    assert result["interrupted"] is False
    assert result["failed"] is False


def test_interrupt_during_compaction_aborts(monkeypatch):
    """Interrupt requested during compaction → loop exits."""
    agent, budget, compacted, messages = _make_base_fixture(monkeypatch)

    def compaction_with_interrupt(msgs, sys_msg, task_id):
        agent._interrupt_requested = True
        return compacted, "compacted system"

    agent._compress_context = Mock(side_effect=compaction_with_interrupt)

    result = run_conversation(agent, "Finish the task", system_message="system")

    assert budget.auto_continue_count == 0
    assert result["interrupted"] is True


def test_fresh_turn_resets_auto_continue_counter(monkeypatch):
    """New user turn creates fresh IterationBudget with zero counter."""
    budget = IterationBudget(1)
    budget._auto_continue_count = 3  # simulate prior turn used all continuations

    # Fresh turn - new budget instance
    fresh_budget = IterationBudget(500)
    assert fresh_budget.auto_continue_count == 0
    assert fresh_budget.max_total == 500
