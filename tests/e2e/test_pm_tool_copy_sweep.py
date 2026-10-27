"""Killed E2E runs skip the ``atexit`` cleanup of their ``pm-e2e-tools-*`` copy; the next import sweeps it."""

import os
import time

from tests.e2e.core._pm_dependencies import sweep_stale_tool_copies

DAY = 24 * 3600


def _aged(path, age_s):
    path.mkdir()
    (path / "payload").write_text("x")
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))


def test_stale_copy_is_removed_and_recent_copy_is_kept(tmp_path):
    stale, recent = tmp_path / "pm-e2e-tools-stale", tmp_path / "pm-e2e-tools-recent"
    _aged(stale, 2 * DAY)
    _aged(recent, 60)

    removed = sweep_stale_tool_copies(tmp_path)

    assert removed == 1
    assert not stale.exists()
    assert recent.exists()


def test_unrelated_old_entries_are_left_alone(tmp_path):
    other_dir, other_file = tmp_path / "keep-me", tmp_path / "pm-e2e-tools-not-a-dir"
    _aged(other_dir, 5 * DAY)
    other_file.write_text("x")
    os.utime(other_file, (time.time() - 5 * DAY,) * 2)

    assert sweep_stale_tool_copies(tmp_path) == 0
    assert other_dir.exists() and other_file.exists()


def test_missing_root_removes_nothing(tmp_path):
    assert sweep_stale_tool_copies(tmp_path / "absent") == 0
