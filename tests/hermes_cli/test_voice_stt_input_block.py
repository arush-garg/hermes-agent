"""CLI voice turns send the transcript to the model inside an STT-flagged <input> block; typed turns never do."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import cli


def _run_turn(voice: bool):
    calls = []
    the_cli = cli.HermesCLI.__new__(cli.HermesCLI)
    the_cli.session_id = "s-voice"
    the_cli.conversation_history = []
    the_cli.agent = SimpleNamespace(run_conversation=Mock(side_effect=lambda **kw: calls.append(kw) or {}))
    turn = cli._ChatTurn()
    if voice:
        turn.voice_prefix = "[Voice input — respond concisely.] "
    the_cli._chat_run_agent(turn, "open the read me file")
    return calls[0]


def test_voice_turn_wraps_transcript_and_persists_clean_text():
    call = _run_turn(voice=True)

    assert '<input source="speech-to-text">' in call["user_message"]
    assert call["user_message"].rstrip().endswith("open the read me file\n</input>")
    assert call["persist_user_message"] == "open the read me file"


def test_typed_turn_is_sent_unwrapped():
    call = _run_turn(voice=False)

    assert "<input" not in call["user_message"]
    assert call["user_message"] == "open the read me file"
