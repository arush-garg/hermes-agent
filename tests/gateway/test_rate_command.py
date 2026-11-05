"""/rate on the gateway: messaging chats rate their session-store session, local sessions their own id."""

import json
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from hermes_cli import ratings


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    return h


def _event(text, platform=Platform.TELEGRAM, chat_id="67890"):
    source = SessionSource(platform=platform, user_id="12345", chat_id=chat_id, user_name="u")
    return MessageEvent(text=text, source=source)


def _runner():
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    entry = MagicMock()
    entry.session_id = "tg_session_1"
    store = MagicMock()
    store.get_or_create_session.return_value = entry
    runner.session_store = store
    return runner


def _rows(home):
    return [json.loads(line) for line in (home / ratings.RATINGS_FILENAME).read_text().splitlines()]


@pytest.mark.asyncio
async def test_messaging_rating_saved_against_session_store_session(home):
    reply = await _runner()._handle_rate_command(_event("/rate 2 forgot the tests"))
    assert "2/5" in reply
    [row] = _rows(home)
    assert row["session_id"] == "tg_session_1"
    assert row["comment"] == "forgot the tests"
    assert row["surface"] == "telegram"


@pytest.mark.asyncio
async def test_local_session_rates_its_own_id(home):
    runner = _runner()
    reply = await runner._handle_rate_command(_event("/rate 5", platform=Platform.LOCAL, chat_id="local_abc"))
    assert "5/5" in reply
    assert _rows(home)[0]["session_id"] == "local_abc"
    runner.session_store.get_or_create_session.assert_not_called()


@pytest.mark.asyncio
async def test_bare_rate_shows_usage_without_touching_session(home):
    runner = _runner()
    reply = await runner._handle_rate_command(_event("/rate"))
    assert "Usage" in reply
    runner.session_store.get_or_create_session.assert_not_called()
    assert not (home / ratings.RATINGS_FILENAME).exists()


def test_rate_is_registered_for_gateway_and_busy_dispatch():
    from hermes_cli.commands import resolve_command
    cmd = resolve_command("rate")
    assert cmd is not None and not cmd.cli_only
    assert cmd.busy_policy == "dispatch"
