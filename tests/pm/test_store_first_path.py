"""store_first_path: current store dirs first, superseded store versions dropped."""

from __future__ import annotations

import os

from pm import install, paths


def _setup(monkeypatch, tmp_path, current):
    store = tmp_path / "tools"
    monkeypatch.setattr(paths, "store_root", lambda: store)
    monkeypatch.setattr(install, "_store_path_dirs", lambda: [str(store / d) for d in current])
    return store


def test_superseded_store_version_is_dropped(monkeypatch, tmp_path):
    store = _setup(monkeypatch, tmp_path, ["node-26.12.1/bin"])
    path = os.pathsep.join([str(store / "node-26.7.0/bin"), "/usr/bin"])

    result = install.store_first_path(path).split(os.pathsep)

    assert result == [str(store / "node-26.12.1/bin"), "/usr/bin"]


def test_non_store_entries_keep_their_order(monkeypatch, tmp_path):
    store = _setup(monkeypatch, tmp_path, ["rg"])
    path = os.pathsep.join(["/opt/homebrew/bin", str(tmp_path / "toolsx" / "bin"), "/usr/bin"])

    result = install.store_first_path(path).split(os.pathsep)

    assert result == [str(store / "rg"), "/opt/homebrew/bin", str(tmp_path / "toolsx" / "bin"), "/usr/bin"]


def test_no_installed_store_leaves_path_untouched(monkeypatch, tmp_path):
    store = _setup(monkeypatch, tmp_path, [])
    path = os.pathsep.join([str(store / "node-26.7.0/bin"), "/usr/bin"])

    assert install.store_first_path(path) == path
