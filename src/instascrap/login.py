"""The login screen: a visible Chrome on a virtual display, reachable through
noVNC, opened only while the Instagram session needs a human.

instascrap never handles an Instagram password. A person signs in by hand,
2FA and "was this you?" checks included, and every later run reuses the
persistent profile that produces. Signing in from a script is precisely the
pattern that gets an account flagged."""

import logging
import os
import shutil
import socket
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .alerts import Alerter, alert_blocked
from .browser import BlockedError, Session
from .config import Config
from .store import Store

log = logging.getLogger(__name__)

LOGIN_URL = "https://www.instagram.com/accounts/login/"
REMIND_EVERY = timedelta(hours=24)
DISPLAY = ":99"
X_SOCKET = Path("/tmp/.X11-unix/X99")
X_LOCK = Path("/tmp/.X99-lock")
VNC_PORT = 5900
NOVNC_PORT = 6080
STARTUP_TIMEOUT_S = 15
STOP_TIMEOUT_S = 5


# Pages a person is still on while signing in: password, 2FA code entry (old
# and new flows), "was this you?" checks.
AUTH_PATHS = ("/accounts/login", "/challenge", "/auth_platform", "/accounts/onetap")


def is_logged_in(cookie_names: set[str], url: str) -> bool:
    # The cookie alone is not enough: Instagram sets sessionid before the 2FA
    # or challenge step is cleared, and the page is still on one of those.
    # ds_user_id only appears once the account is actually signed in.
    return ({"sessionid", "ds_user_id"} <= cookie_names
            and not any(path in url for path in AUTH_PATHS))


def vnc_url(cfg: Config) -> str:
    host = cfg.host_hostname or "<your-server-ip>"
    return f"http://{host}:{NOVNC_PORT}/vnc.html"


def _port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _wait_for(description: str, is_ready, timeout_s: float = STARTUP_TIMEOUT_S) -> None:
    # Xvfb, x11vnc and websockify each need the previous one up. Polling each
    # precondition with a bound turns a startup failure into a named error
    # instead of a blank VNC screen -- a mute failure here invites retries,
    # and repeated login attempts are what gets an account flagged.
    deadline = time.monotonic() + timeout_s
    while not is_ready():
        if time.monotonic() >= deadline:
            raise RuntimeError(f"login screen: timed out after {timeout_s}s waiting for {description}")
        time.sleep(0.2)


class LoginScreen:
    """Xvfb + x11vnc (password-protected, bound to localhost) + websockify/noVNC
    on port 6080, for exactly the duration of the `with` block."""

    def __init__(self, password: str):
        self.password = password
        self._procs: list[subprocess.Popen] = []
        self._tmpdir: str | None = None
        self._previous_display: str | None = None

    def __enter__(self) -> "LoginScreen":
        # Leftovers of an Xvfb that did not exit cleanly would make the new one
        # refuse display :99. Nothing else in the container uses that display.
        for stale in (X_LOCK, X_SOCKET):
            stale.unlink(missing_ok=True)
        # mkdtemp creates the directory 0700. The password goes through a file,
        # never argv, which any process in the container can read; the "rm:"
        # prefix makes x11vnc delete the file once it has read it.
        self._tmpdir = tempfile.mkdtemp(prefix="instascrap-vnc-")
        passfile = Path(self._tmpdir) / "passwd"
        passfile.write_text(self.password + "\n", encoding="utf-8")
        passfile.chmod(0o600)
        try:
            self._spawn(["Xvfb", DISPLAY, "-screen", "0", "1440x900x24", "-nolisten", "tcp"])
            _wait_for("the X display socket", X_SOCKET.exists)
            # -localhost: only websockify, in this container, may reach the raw
            # VNC port. noVNC on 6080 is the single way in.
            self._spawn(["x11vnc", "-display", DISPLAY, "-passwdfile", f"rm:{passfile}",
                         "-forever", "-shared", "-localhost", "-quiet"])
            _wait_for(f"x11vnc on port {VNC_PORT}", lambda: _port_open(VNC_PORT))
            self._spawn(["websockify", "--web=/usr/share/novnc", str(NOVNC_PORT),
                         f"localhost:{VNC_PORT}"])
            _wait_for(f"noVNC on port {NOVNC_PORT}", lambda: _port_open(NOVNC_PORT))
        except BaseException:
            self._stop()
            raise
        self._previous_display = os.environ.get("DISPLAY")
        os.environ["DISPLAY"] = DISPLAY
        return self

    def __exit__(self, *exc) -> None:
        if self._previous_display is None:
            os.environ.pop("DISPLAY", None)
        else:
            os.environ["DISPLAY"] = self._previous_display
        self._stop()

    def _spawn(self, cmd: list[str]) -> None:
        self._procs.append(subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL))

    def _stop(self) -> None:
        for proc in reversed(self._procs):
            proc.terminate()
        for proc in reversed(self._procs):
            try:
                proc.wait(timeout=STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        self._procs.clear()
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def run_login(cfg: Config, store: Store, alerter: Alerter, *, clock=_utcnow, sleep=time.sleep,
              screen_factory=LoginScreen, session_factory=Session, poll_s: float = 5.0) -> None:
    """Open the login screen and wait until a person has signed in.

    Returns when the session is valid (login state cleared), when Instagram
    throttles during sign-in (backoff recorded, login still required), or
    straight away during a backoff: opening a browser on an account that is
    refusing is the "insist after a refusal" this project exists to avoid."""
    now = clock()
    until = store.blocked_until()
    if until and until > now:
        log.warning("login needed, but blocked until %s; not opening the login screen", until)
        return

    reason = store.login_required() or "manual login"
    opening = "No Instagram session yet" if reason == "first run" else "The Instagram session expired"
    message = (f"{opening}. Log in at {vnc_url(cfg)} (the server's IP works too). "
               f"Never expose port {NOVNC_PORT} through your reverse proxy.")

    try:
        with screen_factory(cfg.vnc_password), \
                session_factory(cfg, cfg.data_dir / "profile", headless=False) as session, \
                session.login_mode():
            session.goto(LOGIN_URL)
            while True:
                session.raise_if_blocked()
                if session.ensure_page():
                    session.goto(LOGIN_URL)
                if is_logged_in(session.cookie_names(), session.current_url()):
                    break
                now = clock()
                last = store.last_login_alert()
                # Once per lost session, then once a day. The timestamp lives in
                # the store, not here: the daemon reopens this screen hourly when
                # it fails, and each reopening must not alert again.
                if last is None or now - last >= REMIND_EVERY:
                    alerter.send("alert", "Instagram login needed", message)
                    store.mark_login_alert(now)
                sleep(poll_s)
            # Responses are only processed during Playwright calls, so a
            # throttle in the post-login burst can land during the very
            # cookie read that ended the loop. Checked once more before
            # declaring success, as the old manual --login did.
            session.raise_if_blocked()
    except BlockedError as exc:
        # login_mode drops LoggedOutError, so anything landing here is a real
        # throttle. The login stays required; the backoff decides when to retry.
        blocked_until = store.record_block(str(exc), clock())
        alert_blocked(alerter, str(exc), blocked_until, cfg.tz)
        return

    store.clear_login()
    alerter.send("info", "Logged in", "Instagram session restored. Runs resume at the next scheduled time.")
