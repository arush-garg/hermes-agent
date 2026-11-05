"""/rate — end-of-session 1-5 rating plus optional feedback, appended to ratings.jsonl."""

import json

import pytest

from hermes_cli import ratings


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    return h


def _rows(home):
    path = home / ratings.RATINGS_FILENAME
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.mark.parametrize("raw, expected", [
    ("5", (5, "")),
    ("  3   ", (3, "")),
    ("4 great at refactors, slow on tests", (4, "great at refactors, slow on tests")),
    ("2/5 ignored my instructions", (2, "ignored my instructions")),
    ("1 - kept looping", (1, "kept looping")),
])
def test_parse_accepts_score_and_optional_comment(raw, expected):
    assert ratings.parse_rate_args(raw) == expected


@pytest.mark.parametrize("raw", ["", "0", "6", "great", "4.5 nice", "-1"])
def test_parse_rejects_missing_or_out_of_range_score(raw):
    with pytest.raises(ValueError):
        ratings.parse_rate_args(raw)


def test_record_appends_one_json_line_per_rating(home):
    ratings.record_rating("s1", 4, "good", surface="cli", model="m1")
    ratings.record_rating("s1", 2, "", surface="telegram")

    rows = _rows(home)
    assert [r["rating"] for r in rows] == [4, 2]
    assert rows[0]["session_id"] == "s1"
    assert rows[0]["comment"] == "good"
    assert rows[0]["model"] == "m1"
    assert rows[1]["surface"] == "telegram"
    assert "model" not in rows[1]
    assert all(isinstance(r["ts"], float) for r in rows)


def test_record_caps_comment_length(home):
    ratings.record_rating("s1", 3, "x" * (ratings.MAX_COMMENT_CHARS + 50), surface="cli")
    assert len(_rows(home)[0]["comment"]) == ratings.MAX_COMMENT_CHARS


def test_record_requires_session_id(home):
    with pytest.raises(ValueError):
        ratings.record_rating("", 3, "", surface="cli")


def test_load_returns_latest_rating_per_session(home):
    ratings.record_rating("s1", 1, "bad", surface="cli")
    ratings.record_rating("s2", 5, "", surface="cli")
    ratings.record_rating("s1", 3, "on reflection ok", surface="cli")

    latest = ratings.latest_by_session(ratings.load_ratings())
    assert latest["s1"]["rating"] == 3
    assert latest["s1"]["comment"] == "on reflection ok"
    assert latest["s2"]["rating"] == 5


def test_load_skips_corrupt_lines(home):
    ratings.record_rating("s1", 4, "", surface="cli")
    with (home / ratings.RATINGS_FILENAME).open("a") as fh:
        fh.write("{not json\n")
        fh.write(json.dumps({"session_id": "s2", "rating": 9}) + "\n")  # invalid score
    assert [r["session_id"] for r in ratings.load_ratings()] == ["s1"]


def test_load_missing_file_is_empty(home):
    assert ratings.load_ratings() == []


def test_run_command_saves_and_confirms(home):
    reply = ratings.run_rate_command("5 nailed it", session_id="s1", surface="cli")
    assert "5/5" in reply
    assert _rows(home)[0]["comment"] == "nailed it"


def test_run_command_without_args_shows_usage_and_saves_nothing(home):
    reply = ratings.run_rate_command("", session_id="s1", surface="cli")
    assert "Usage" in reply
    assert not (home / ratings.RATINGS_FILENAME).exists()


def test_run_command_without_session_explains(home):
    reply = ratings.run_rate_command("4", session_id=None, surface="cli")
    assert "no active session" in reply.lower()
    assert not (home / ratings.RATINGS_FILENAME).exists()
