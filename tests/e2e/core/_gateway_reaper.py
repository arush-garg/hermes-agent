"""Stop the gateway a test's CLI turn auto-started.

A client turn auto-starts its home's gateway, which then idles for
``gateway.unmanaged_idle_exit_seconds`` (600s) after the turn; across a sweep those pile up
(one ~120 MB process per test home).
"""

from __future__ import annotations

from pathlib import Path


def stop_home_gateway(hermes_home: Path, timeout: float = 30.0) -> None:
    """Terminate ``hermes_home``'s gateway, if one is live. Only the PID the gateway's own identity
    check proves is that home's gateway is signalled, so a recycled PID is left alone."""
    import psutil
    from gateway.status import live_gateway_pid_for_home

    pid = live_gateway_pid_for_home(hermes_home)
    if pid is None:
        return
    try:
        gateway = psutil.Process(pid)  # binds the create time: terminate() refuses a reused PID
        gateway.terminate()
        gateway.wait(timeout=timeout)
    except psutil.NoSuchProcess:
        return
    except psutil.TimeoutExpired:
        gateway.kill()
