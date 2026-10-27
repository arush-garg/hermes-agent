"""Worker cap for e2e thread pools.

Each pool worker drives a real hermes CLI plus the gateway it auto-starts (~400 MB together), so
an 8-wide pool is fine on a CI runner but swamps a developer machine that also runs a live Hermes.
``HERMES_E2E_MAX_WORKERS`` caps every pool; unset keeps each pool's own width.
"""

from __future__ import annotations

import os


def pool_workers(wanted: int) -> int:
    try:
        cap = int(os.environ.get("HERMES_E2E_MAX_WORKERS", ""))
    except ValueError:
        return max(1, wanted)
    return max(1, min(wanted, cap))
