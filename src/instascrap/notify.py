"""Unraid notifications. Silent failure is the enemy: a broken parser would
otherwise show up as feeds that quietly stop growing, discovered weeks later."""

import logging
import subprocess
from pathlib import Path

NOTIFY_SCRIPT = Path("/usr/local/emhttp/webGui/scripts/notify")

log = logging.getLogger(__name__)


def notify(summary: str, description: str, level: str = "warning") -> bool:
    if not NOTIFY_SCRIPT.exists():
        log.warning("notification (no Unraid host): %s — %s", summary, description)
        return False

    try:
        subprocess.run(
            [str(NOTIFY_SCRIPT), "-e", "instascrap", "-s", summary,
             "-d", description, "-i", level],
            check=False,
        )
    except OSError as e:
        # Catch PermissionError, FileNotFoundError, and other exec failures.
        # Fall back to logging rather than propagate, so notify() never becomes
        # a second failure point in the exception handler.
        log.warning("notification exec failed: %s — %s (%s)", summary, description, e)
        return False
    return True
