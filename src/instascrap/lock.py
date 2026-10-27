"""One Chrome at a time on the Instagram profile.

The daemon orders its own runs and logins, but `docker exec ... --run` starts a
second process. Two Chromes on one persistent profile corrupt or lock it, which
costs the Instagram session, and a forced re-login is the repeated-login
pattern that gets an account flagged. Every mode that opens the profile takes
this lock first."""

import contextlib
import fcntl
from pathlib import Path

LOCK_NAME = ".session.lock"


class SessionBusy(RuntimeError):
    """Another process is using the Chrome profile."""


@contextlib.contextmanager
def session_lock(data_dir: Path, wait: bool = True):
    data_dir.mkdir(parents=True, exist_ok=True)
    with open(data_dir / LOCK_NAME, "a") as handle:
        flags = fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(handle, flags)
        except BlockingIOError:
            raise SessionBusy("another instascrap process is using the Chrome profile "
                              "(a scheduled run or the login screen)") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
