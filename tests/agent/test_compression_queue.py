"""Tests for queuing user messages during context compression.

Messages sent by the user while compression is in-flight should be queued
and injected into the compressed transcript as new user turns after
compression completes successfully.
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from agent.conversation_compression import compress_context


class MockCompressor:
    """Mock compressor that simulates a slow compression with a callback."""
    
    def __init__(self, delay=0.1, should_succeed=True, compressed_messages=None):
        self.delay = delay
        self.should_succeed = should_succeed
        self._compressed_messages = compressed_messages or [
            {"role": "user", "content": "old message 1"},
            {"role": "assistant", "content": "old response 1"},
            {"role": "user", "content": "old message 2"},
            {"role": "assistant", "content": "old response 2"},
        ]
        self.compression_count = 0
        self.context_length = 128000
        self.threshold_tokens = 64000
        self._last_compress_aborted = False
        self._last_summary_error = None
        self._last_aux_model_failure_model = None
        self._last_aux_model_failure_error = None
    
    def compress(self, messages, current_tokens=None, focus_topic=None, force=False):
        time.sleep(self.delay)
        self.compression_count += 1
        if not self.should_succeed:
            self._last_compress_aborted = True
            self._last_summary_error = "simulated failure"
            return messages  # return unchanged on failure
        self._last_compress_aborted = False
        # Return compressed messages (simulating summary + retained tail)
        return self._compressed_messages


class MockAgent:
    """Mock agent with minimal required attributes."""
    
    def __init__(self, compressor=None, session_id="test-session"):
        self.session_id = session_id
        self.context_compressor = compressor or MockCompressor()
        self._cached_system_prompt = "system prompt"
        self._session_db = None
        self._memory_manager = None
        self._todo_store = MagicMock()
        self._todo_store.format_for_injection.return_value = None
        self._flushed_db_message_ids = set()
        self._last_flushed_db_idx = 0
        self.log_prefix = "[TEST] "
        self.platform = "cli"
        self.model = "test-model"
        self.tools = []
        self._compression_warning = None
        self._last_compaction_in_place = False
        self._pending_compression_queue = []
        self._pending_compression_lock = threading.Lock()
        self._cached_system_prompt = None
        self._session_init_model_config = {}
        self._session_db_created = False
        self.provider = ""
        self._custom_providers = {}
        self.compression_enabled = True
        self.compression_in_place = False
        self._compression_feasibility_checked = False
        self._last_compression_summary_warning = None
        self._last_compression_lock_error_sid = None
        self._last_aux_fallback_warning_key = None
    
    def _build_system_prompt(self, system_message):
        return system_message or "system prompt"
    
    def _invalidate_system_prompt(self):
        pass
    
    def _emit_status(self, msg):
        pass
    
    def _emit_warning(self, msg):
        pass
    
    def commit_memory_session(self, messages):
        pass
    
    def queue_message_during_compression(self, text: str) -> bool:
        """Queue a user message received while compression is in flight."""
        if not text or not text.strip():
            return False
        cleaned = text.strip()
        _lock = getattr(self, "_pending_compression_lock", None)
        if _lock is None:
            self._pending_compression_queue.append(cleaned)
            return True
        with _lock:
            self._pending_compression_queue.append(cleaned)
        return True
    
    def _drain_compression_queue(self) -> list:
        """Drain and return all messages queued during compression."""
        _lock = getattr(self, "_pending_compression_lock", None)
        if _lock is None:
            queued = getattr(self, "_pending_compression_queue", [])
            self._pending_compression_queue = []
            return queued
        with _lock:
            queued = list(self._pending_compression_queue)
            self._pending_compression_queue.clear()
        return queued


def test_compression_queue_injected_after_successful_compression():
    """Messages queued during compression appear in compressed transcript."""
    agent = MockAgent()
    compressor = MockCompressor(delay=0.05)
    agent.context_compressor = compressor
    
    messages = [
        {"role": "user", "content": "old message 1"},
        {"role": "assistant", "content": "old response 1"},
        {"role": "user", "content": "old message 2"},
        {"role": "assistant", "content": "old response 2"},
    ]
    
    # Start compression in background thread
    result_container = {}
    def run_compression():
        result_container["result"] = compress_context(
            agent, messages, "system prompt", approx_tokens=10000, task_id="test"
        )
    
    comp_thread = threading.Thread(target=run_compression)
    comp_thread.start()
    
    # Give compression time to start
    time.sleep(0.01)
    
    # Queue messages during compression
    agent.queue_message_during_compression("queued message 1")
    agent.queue_message_during_compression("queued message 2")
    
    # Wait for compression to complete
    comp_thread.join(timeout=2.0)
    
    compressed_messages, new_system_prompt = result_container["result"]
    
    # Verify queued messages were injected
    user_messages = [m for m in compressed_messages if m["role"] == "user"]
    assert any("queued message 1" in m["content"] for m in user_messages)
    assert any("queued message 2" in m["content"] for m in user_messages)
    
    # Should be in order
    queued_idx = [i for i, m in enumerate(compressed_messages) if "queued message" in m.get("content", "")]
    assert queued_idx[0] < queued_idx[1]


def test_compression_queue_not_injected_on_failed_compression():
    """Messages queued during failed compression are NOT injected."""
    agent = MockAgent()
    compressor = MockCompressor(delay=0.05, should_succeed=False)
    agent.context_compressor = compressor
    
    messages = [
        {"role": "user", "content": "old message 1"},
        {"role": "assistant", "content": "old response 1"},
    ]
    
    result_container = {}
    def run_compression():
        result_container["result"] = compress_context(
            agent, messages, "system prompt", approx_tokens=10000, task_id="test"
        )
    
    comp_thread = threading.Thread(target=run_compression)
    comp_thread.start()
    
    time.sleep(0.01)
    
    # Queue messages during compression
    agent.queue_message_during_compression("queued message 1")
    agent.queue_message_during_compression("queued message 2")
    
    comp_thread.join(timeout=2.0)
    
    compressed_messages, new_system_prompt = result_container["result"]
    
    # Compression failed, so original messages returned unchanged
    assert len(compressed_messages) == len(messages)
    
    # Queued messages should have been cleared but NOT injected
    queued = agent._drain_compression_queue()
    assert len(queued) == 0  # queue cleared on failure


def test_compression_queue_thread_safety():
    """Queue operations are thread-safe under concurrent access."""
    agent = MockAgent()
    errors = []
    
    def writer(msg_id):
        try:
            for i in range(10):
                agent.queue_message_during_compression(f"msg-{msg_id}-{i}")
                time.sleep(0.001)
        except Exception as e:
            errors.append(e)
    
    def reader():
        try:
            for _ in range(20):
                agent._drain_compression_queue()
                time.sleep(0.001)
        except Exception as e:
            errors.append(e)
    
    threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
    threads.append(threading.Thread(target=reader))
    
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=2.0)
    
    assert not errors, f"Thread safety errors: {errors}"


def test_compression_queue_preserves_alternation():
    """Injected messages preserve user/assistant role alternation."""
    # Compressed transcript ends with assistant
    compressor = MockCompressor(
        compressed_messages=[
            {"role": "user", "content": "old 1"},
            {"role": "assistant", "content": "old 2"},
            {"role": "user", "content": "old 3"},
            {"role": "assistant", "content": "old 4"},  # ends with assistant
        ]
    )
    agent = MockAgent(compressor=compressor)
    
    messages = [
        {"role": "user", "content": "old 1"},
        {"role": "assistant", "content": "old 2"},
        {"role": "user", "content": "old 3"},
        {"role": "assistant", "content": "old 4"},
    ]
    
    result_container = {}
    def run_compression():
        result_container["result"] = compress_context(
            agent, messages, "system prompt", approx_tokens=10000, task_id="test"
        )
    
    comp_thread = threading.Thread(target=run_compression)
    comp_thread.start()
    time.sleep(0.01)
    
    # Queue multiple messages
    agent.queue_message_during_compression("queued 1")
    agent.queue_message_during_compression("queued 2")
    agent.queue_message_during_compression("queued 3")
    
    comp_thread.join(timeout=2.0)
    
    compressed_messages, _ = result_container["result"]
    
    # Check role alternation in the injected portion
    roles = [m["role"] for m in compressed_messages]
    # Should not have consecutive 'user' roles
    for i in range(len(roles) - 1):
        assert not (roles[i] == "user" and roles[i+1] == "user"), \
            f"Consecutive user roles at index {i}: {roles[i:i+3]}"


def test_compression_queue_empty_messages_ignored():
    """Empty/whitespace messages are not queued."""
    agent = MockAgent()
    
    assert agent.queue_message_during_compression("") is False
    assert agent.queue_message_during_compression("   ") is False
    assert agent.queue_message_during_compression("\n\t") is False
    assert agent.queue_message_during_compression("real message") is True
    
    queued = agent._drain_compression_queue()
    assert len(queued) == 1
    assert queued[0] == "real message"


if __name__ == "__main__":
    pytest.main([__file__, "-xvs"])