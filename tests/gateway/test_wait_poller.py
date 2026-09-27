"""/wait injection through the gateway heartbeat poller: the user's own deferred message, sent once when idle."""

import asyncio
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.response_filters import display_kind_for_event, is_scheduled_heartbeat_event
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
from hermes_cli.waits import WaitManager, load_waits, run_wait_command


class _Adapter(BasePlatformAdapter):
    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        pass

    async def get_chat_info(self, chat_id):
        return {}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="wire-1")


@pytest.fixture
def poller(monkeypatch):
    from hermes_cli import goals, waits

    goals._DB_CACHE.clear()
    goals._get_session_db()
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(waits, "time", SimpleNamespace(time=lambda: clock.now))
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", user_id="42", chat_type="dm", message_id="cmd")
    key = build_session_key(source)
    adapter = _Adapter(PlatformConfig(enabled=True, typing_indicator=False), Platform.TELEGRAM)
    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._delivery_adapter_for = lambda source: adapter
    runner._run_in_executor_with_context = asyncio.to_thread
    watch = {key: (source, "wait-session")}
    yield runner, adapter, watch, key, clock
    goals._DB_CACHE.clear()


@pytest.mark.asyncio
async def test_due_wait_is_sent_once_as_a_plain_user_turn(poller):
    runner, adapter, watch, key, clock = poller
    received = []

    async def handler(event):
        event._heartbeat_execution_started = True  # fake agent execution boundary
        received.append(event)
        return None

    adapter.set_message_handler(handler)
    assert run_wait_command(WaitManager("wait-session"), "2h continue working on this")[1]
    clock.now += 7200 - 10
    await runner._heartbeat_poll_once(watch)
    assert not received
    clock.now += 20
    for _ in range(3):
        await runner._heartbeat_poll_once(watch)
        await asyncio.gather(*adapter._background_tasks)
    assert [e.text for e in received] == ["continue working on this"]
    event = received[0]
    # The user's own message: a normal visible turn, not a quiet machinery heartbeat row.
    assert not event.internal and not is_scheduled_heartbeat_event(event) and display_kind_for_event(event) is None
    assert event.source.message_id is None
    assert key not in watch  # nothing left to wait for


@pytest.mark.asyncio
async def test_wait_whose_turn_never_starts_stays_due(poller):
    runner, adapter, watch, key, clock = poller

    async def handler(event):
        return None  # admission refused before the agent runner (e.g. owner changed)

    adapter.set_message_handler(handler)
    WaitManager("wait-session").add("ping", 60)
    clock.now += 61
    await runner._heartbeat_poll_once(watch)
    await asyncio.gather(*adapter._background_tasks)
    assert [w.prompt for w in load_waits("wait-session").waits] == ["ping"]
    assert key in watch
