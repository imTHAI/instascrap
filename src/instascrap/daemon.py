"""--serve: the container's long-running process.

It replaces what used to live on the host (a cron entry, run.sh's jitter and
lock, login.sh): one process orders runs and logins, so the two can never
overlap on the Chrome profile, and it keeps the Caddy that serves the feeds
alive."""

import logging
import os
import random
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .alerts import Alerter
from .config import Config
from .feed import publish as publish_feeds
from .login import run_login
from .main import run_once
from .schedule import next_slot
from .store import Store

log = logging.getLogger(__name__)

CADDY_CMD = ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
CADDY_RESTART_MIN_S = 10
# Sleeps are cut into slices so a dead Caddy is noticed within a minute rather
# than at the next run, hours later.
SLEEP_SLICE_S = 60
LOGIN_RETRY = timedelta(hours=1)
# Chrome's single-instance markers. A container killed mid-session leaves them
# behind, pointing at a hostname that no longer exists, and the next Chrome then
# refuses the profile as "in use on another computer". This process is the only
# Chrome user of the profile, so at startup they can only be stale.
PROFILE_LOCKS = ("SingletonLock", "SingletonSocket", "SingletonCookie")


def clear_profile_locks(profile_dir: Path) -> list[str]:
    removed = []
    for name in PROFILE_LOCKS:
        path = profile_dir / name
        # lexists, not exists: these are usually symlinks to nothing.
        if os.path.lexists(path):
            path.unlink()
            removed.append(name)
    return removed


def initial_login_reason(profile_dir: Path, store: Store) -> str | None:
    recorded = store.login_required()
    if recorded:
        return recorded
    # An existing, non-empty profile is trusted as logged in: that is how an
    # install migrated from the User Scripts setup keeps its session. A
    # profile that turns out to be logged out is caught by the first run.
    if not profile_dir.is_dir() or not any(profile_dir.iterdir()):
        return "first run"
    return None


class CaddySupervisor:
    def __init__(self, cmd: list[str], popen=subprocess.Popen, clock=time.monotonic):
        self.cmd = cmd
        self._popen = popen
        self._clock = clock
        self._proc = None
        self._started_at = 0.0

    def start(self) -> None:
        self._proc = self._popen(self.cmd)
        self._started_at = self._clock()

    def poll(self) -> None:
        if self._proc is None:
            return
        code = self._proc.poll()
        if code is None:
            return
        # Rate-limited so a Caddy that dies on startup (port taken, bad config)
        # is retried every few seconds at most, not spun in a tight loop.
        if self._clock() - self._started_at < CADDY_RESTART_MIN_S:
            return
        log.warning("caddy exited with %s, restarting", code)
        self.start()

    def stop(self) -> None:
        if self._proc is None or self._proc.poll() is not None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def serve(cfg: Config, *, clock=_utcnow, sleep=time.sleep, rng: random.Random | None = None,
          caddy=None, login=run_login, run=run_once, publish=publish_feeds,
          max_iterations: int | None = None) -> None:
    rng = rng or random.Random()
    caddy = caddy or CaddySupervisor(CADDY_CMD)
    store = Store(cfg.data_dir)
    alerter = Alerter(cfg.discord_webhook)
    tz = ZoneInfo(cfg.tz)
    profile_dir = cfg.data_dir / "profile"

    def wait_until(deadline: datetime) -> None:
        while (remaining := (deadline - clock()).total_seconds()) > 0:
            sleep(min(remaining, SLEEP_SLICE_S))
            caddy.poll()

    removed = clear_profile_locks(profile_dir)
    if removed:
        log.info("removed stale Chrome profile locks: %s", ", ".join(removed))

    caddy.start()
    try:
        try:
            # Regenerated at every start, so a changed PUBLIC_URL, FEED_ITEMS or
            # account list shows up without waiting for the next run.
            publish(store, cfg)
        except Exception:  # noqa: BLE001 -- stale feeds beat a container that will not start
            log.exception("could not publish feeds at startup")
        log.info("Feeds index: %s/%s/index.html", cfg.public_base_url, cfg.public_prefix)

        reason = initial_login_reason(profile_dir, store)
        if reason and store.login_required() is None:
            store.require_login(reason, clock())

        iterations = 0
        while max_iterations is None or iterations < max_iterations:
            iterations += 1

            if store.login_required():
                try:
                    login(cfg, store, alerter)
                except Exception as exc:  # noqa: BLE001 -- e.g. Xvfb failed to start
                    log.exception("login screen failed")
                    alerter.send("alert", "Login screen failed", str(exc))
                if store.login_required():
                    # Unresolved: either a backoff refused to open the screen or
                    # the screen failed. Retry within the hour, or as soon as the
                    # backoff ends.
                    retry_at = clock() + LOGIN_RETRY
                    until = store.blocked_until()
                    if until and clock() < until < retry_at:
                        retry_at = until
                    wait_until(retry_at)
                continue

            slot = next_slot(clock(), cfg.schedule, tz, timedelta(minutes=cfg.jitter_minutes), rng)
            log.info("next run at %s", slot.astimezone(tz).isoformat(timespec="minutes"))
            wait_until(slot)

            now = clock()
            until = store.blocked_until()
            if until and until > now:
                log.warning("blocked until %s, skipping", until)
                continue
            try:
                run(cfg, store, alerter, now)
            except Exception:  # noqa: BLE001 -- run_once has already alerted
                log.warning("run failed; waiting for the next slot")
    finally:
        caddy.stop()
