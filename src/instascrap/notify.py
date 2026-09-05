"""Unraid notifications — the dev-host fallback only.

In production this path is structurally dead and that is by design: the notify
script is PHP, needs /usr/local/emhttp/webGui/include and an interpreter, and
none of that exists inside the Playwright image, so every call here lands in the
"script absent" branch and logs. The notification that actually reaches the user
is raised by deploy/run.sh on the host, from the exit code and last_run.json.

What this module is still for: running the code outside a container (on the
Unraid box itself, or on a dev machine where it logs), so a manual --run or
--capture is not silent."""

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
