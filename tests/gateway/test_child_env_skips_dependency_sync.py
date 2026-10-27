"""Daemon/gateway children started under a fresh test HERMES_HOME must not build a dependency environment.

On a self-managed install a launch under another data root borrows the checkout and syncs an environment of
its own (#123238, minutes cold). Test children get a whitelisted env, so the lazy-install kill-switch has to
be on that whitelist or every spawned daemon times out."""

import os
import subprocess
import sys
from pathlib import Path

from tests.gateway.fixtures.local_recovery_probe import child_env

ROOT = Path(__file__).resolve().parents[2]


def test_shared_child_env_carries_the_lazy_install_kill_switch(monkeypatch):
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")

    assert child_env()["HERMES_DISABLE_LAZY_INSTALLS"] == "1"


def test_bootstrap_import_under_a_fresh_home_is_quick_with_the_kill_switch(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {k: os.environ[k] for k in ("PATH", "LANG", "TZ") if k in os.environ}
    env.update(HOME=str(tmp_path), HERMES_HOME=str(home), PYTHONPATH=str(ROOT), HERMES_DISABLE_LAZY_INSTALLS="1")

    done = subprocess.run([sys.executable, "-c", "import hermes_bootstrap"], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=60, check=False)

    assert done.returncode == 0, done.stderr
    assert "preparing dependencies" not in done.stderr
