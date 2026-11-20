"""The vision_analyze completion row names the route the image actually took."""

import json

from agent.display import get_cute_tool_message
from tools.vision_tools import _build_native_vision_tool_result

ARGS = {"image_url": "/tmp/reel/sheet.jpg", "question": "List the repo names on screen"}


def test_native_attach_row_shows_view_and_file_not_question():
    result = _build_native_vision_tool_result(ARGS["image_url"], ARGS["question"], "data:image/jpeg;base64,AAAA", 2048)
    line = get_cute_tool_message("vision_analyze", ARGS, 0.1, result=result)
    assert "view" in line and "sheet.jpg" in line
    assert "List the repo names" not in line


def test_aux_analysis_row_keeps_question():
    result = json.dumps({"success": True, "analysis": "Two repos: foo, bar."})
    line = get_cute_tool_message("vision_analyze", ARGS, 0.1, result=result)
    assert "vision" in line and "List the repo names" in line
