"""wrap_stt_input: voice transcripts reach the model inside an <input> block that says they came from STT."""

from tools.voice_mode_transcript import STT_INPUT_NOTE, wrap_stt_input


def test_wraps_plain_string_with_note_and_transcript():
    wrapped = wrap_stt_input("whatever the stt transcription was")

    assert wrapped.startswith('<input source="speech-to-text">\n')
    assert STT_INPUT_NOTE in wrapped
    assert wrapped.endswith("\nwhatever the stt transcription was\n</input>")


def test_strips_surrounding_whitespace_from_transcript():
    assert wrap_stt_input("  hi there \n").endswith("\nhi there\n</input>")


def test_wraps_first_text_part_of_multimodal_message_without_mutating_input():
    image = {"type": "image_url", "image_url": {"url": "data:x"}}
    parts = [image, {"type": "text", "text": "what is this"}]

    wrapped = wrap_stt_input(parts)

    assert wrapped[0] is image
    assert wrapped[1]["text"].endswith("\nwhat is this\n</input>")
    assert parts[1]["text"] == "what is this"


def test_leaves_empty_and_non_text_messages_alone():
    assert wrap_stt_input("") == ""
    assert wrap_stt_input("   ") == "   "
    assert wrap_stt_input(None) is None
