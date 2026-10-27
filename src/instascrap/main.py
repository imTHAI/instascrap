"""Orchestration. The rule that outranks everything else: a block ends the session
on the spot. No retry, no next account."""

import argparse
import json
import logging
import os
import re
import signal
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .browser import BlockedError, LoggedOutError, Session
from .config import Config, ConfigError, load_config
from .feed import publish
from .lock import SessionBusy, session_lock
from .login import run_login
from .alerts import Alerter, alert_blocked
from .parse import extract_posts_counted, grid_owner, has_profile_grid
from .store import Store

log = logging.getLogger(__name__)
STALE_AFTER = timedelta(days=7)

# Exit codes of the one-shot CLI modes (--run, --login, --capture).
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_BLOCKED = 2

LAST_RUN_NAME = "last_run.json"


@dataclass
class SessionResult:
    visited: list[str] = field(default_factory=list)
    new_posts: int = 0
    blocked: str | None = None
    # Accounts whose visit raised something that was not a block. Recorded rather
    # than propagated: one bad account must not cost the rotation.
    failures: list[str] = field(default_factory=list)
    # Accounts whose profile visit produced no payload carrying the profile-grid
    # container at all -- not "zero new posts" (ordinary, silent), but "the
    # thing we came for never showed up". Supersedes an earlier, narrower
    # `empty_visits` that only caught a visit with *no* payload whatsoever: it
    # missed the production case where a stray home-feed payload (left over
    # from warm_up) arrived instead of the grid and made the visit look
    # answered. Every old empty visit is still caught here — zero payloads
    # trivially contains no grid — so nothing is lost by folding the two
    # together, and the account only ever gets one notification about it
    # instead of two describing the same underlying failure.
    grid_missing: list[str] = field(default_factory=list)
    dropped_nodes: int = 0
    # Set when Instagram sent the session to its login or challenge page. Not a
    # block: no backoff is recorded, the session just needs a human again.
    logged_out: str | None = None


def _flat(text: str, limit: int = 300) -> str:
    """One line, no quotes, bounded. last_run.json is read by people grepping a
    container's data directory, and was once parsed with sed: keeping each field
    flat keeps both working."""
    return re.sub(r'[\s"\\]+', " ", str(text)).strip()[:limit]


def _write_last_run(data_dir: Path, **fields) -> None:
    """The outcome of the last run, for manual diagnosis. Written on every path,
    the exception path included, so the file never describes an older run."""
    payload = {"at": datetime.now(timezone.utc).isoformat(), **fields}
    path = Path(data_dir) / LAST_RUN_NAME
    tmp = path.with_name(path.name + ".tmp")
    try:
        # indent + sort_keys is not cosmetic: it puts one field per line, which is
        # what makes the shell-side sed extraction in run.sh reliable.
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("could not write %s: %s", LAST_RUN_NAME, exc)


def _record_block(store: Store, result: SessionResult, exc: BlockedError, now: datetime) -> SessionResult:
    result.blocked = str(exc)
    store.record_block(str(exc), now)
    return result


def _record_logout(store: Store, result: SessionResult, exc: LoggedOutError, now: datetime) -> SessionResult:
    # Deliberately not record_block: a backoff would only delay the moment
    # someone signs in again, and the next block after that login would then
    # escalate straight to 72h for a refusal that never happened.
    log.warning("logged out: %s", exc)
    result.logged_out = str(exc)
    store.require_login(str(exc), now)
    return result


def _fetch_post_media(session, post) -> dict[str, bytes]:
    fetched: dict[str, bytes] = {}
    for media in post.media:
        fetched[media.url] = session.fetch_media(media.url)
        if media.poster_url and media.poster_url not in fetched:
            try:
                fetched[media.poster_url] = session.fetch_media(media.poster_url)
            except BlockedError:
                raise
            except Exception as exc:  # noqa: BLE001 — a cover frame is optional
                # A video without its poster still plays; losing the post over a
                # missing cover frame would be the worse trade.
                log.warning("poster fetch failed for %s: %s", post.shortcode, exc)
    return fetched


def _visit_account(store: Store, session, account: str,
                   now: datetime, result: SessionResult) -> None:
    payloads = session.visit_profile(account)
    if not any(has_profile_grid(p, account) for p in payloads):
        # Session.visit_profile already waits for a grid payload specifically
        # (see its docstring); getting here means that wait gave up — either
        # nothing arrived at all, or only something else did (the stray
        # home-feed payload that masked the michellemonaghan defect). Either
        # way it is an anomaly, never a quiet week: a profile with no *new*
        # posts still receives a grid payload whose edges are all already
        # known, so this can never fire on a genuinely quiet account, only on
        # one whose grid never showed up. Recorded, not raised — one bad
        # profile must not cost the rotation any more than an ordinary
        # failure does.
        log.warning("no profile-grid response for %s (%d payload(s) captured)",
                    account, len(payloads))
        result.grid_missing.append(account)

    # When the visit got its own grid, only grid payloads (own or foreign) are
    # archived: browser.py captures every JSON response from Instagram's host,
    # about fifteen unrelated queries per visit, and keeping those would bloat
    # raw/ with nothing anyone reads. When the grid is missing, everything is
    # archived instead -- the stray leftovers are then the evidence, and are how
    # the empty visits, the wrong-account attribution below and the 2026-10-17
    # endpoint move were all diagnosed. Parsing is the part that discriminates.
    archive_all = account in result.grid_missing
    known = store.known_shortcodes(account)
    for payload in payloads:
        if archive_all or has_profile_grid(payload):
            store.archive_raw(account, payload, now)
        if not has_profile_grid(payload, account):
            # Archived just above when relevant, then skipped here. Two payloads
            # reach this point without belonging to `account`: the harmless
            # non-grid leftovers (a home-feed page from warm_up), which have
            # nothing to extract anyway, and another profile's grid arriving
            # late during this visit -- which extract_posts_counted would
            # happily attribute to `account`, because it stamps the account it
            # is handed onto every node it reads and has no way of knowing
            # better. That is exactly how 48 of the first 75 archived posts
            # ended up in the wrong feed.
            owner = grid_owner(payload)
            if owner is not None:
                log.warning("%s: payload carries %s's grid, not extracted", account, owner)
            continue
        posts, dropped = extract_posts_counted(payload, account)
        if dropped:
            log.warning("%s: %d node(s) dropped as unparseable", account, dropped)
            result.dropped_nodes += dropped
        for post in posts:
            if post.shortcode in known:
                continue
            try:
                store.save_post(post, _fetch_post_media(session, post))
            except BlockedError:
                raise
            except Exception as exc:  # noqa: BLE001 — isolate at post granularity
                log.warning("skipping %s/%s: %s", account, post.shortcode, exc)
                result.failures.append(f"{account}/{post.shortcode}: {exc}")
                continue
            known.add(post.shortcode)
            result.new_posts += 1


def run_session(cfg: Config, store: Store, session, now: datetime) -> SessionResult:
    result = SessionResult()

    until = store.blocked_until()
    if until and until > now:
        log.warning("blocked until %s, doing nothing", until)
        return result
    # An expired block record is deliberately left in place rather than cleared
    # here: if this run gets blocked again immediately, record_block must still
    # see the prior record to escalate 24h -> 72h (consecutive-block backoff).
    # It is cleared only once this run completes without hitting a block.
    had_expired_block = until is not None

    try:
        session.warm_up()
    except LoggedOutError as exc:
        return _record_logout(store, result, exc, now)
    except BlockedError as exc:
        return _record_block(store, result, exc, now)

    for account in store.visit_order(cfg.accounts, cfg.accounts_per_session):
        try:
            _visit_account(store, session, account, now, result)
        except LoggedOutError as exc:
            return _record_logout(store, result, exc, now)
        except BlockedError as exc:
            return _record_block(store, result, exc, now)
        except Exception as exc:  # noqa: BLE001 — see the comment below
            log.exception("account %s failed, moving on", account)
            result.failures.append(f"{account}: {exc}")

        # Marked visited even after a failure. Leaving the timestamp untouched
        # would sort this account first again on the next run, where it would
        # fail at the same URL — and no other account would ever be visited
        # again. That wedge is silent and self-perpetuating, which makes it worse
        # than the missed harvest it is trading against.
        store.mark_visited(account, now)
        result.visited.append(account)

        try:
            session.gap()
        except LoggedOutError as exc:
            return _record_logout(store, result, exc, now)
        except BlockedError as exc:
            return _record_block(store, result, exc, now)

    if had_expired_block:
        store.clear_block()
    if result.new_posts:
        store.mark_new_post(now)
    return result


def _silent_days(store: Store, now: datetime) -> int:
    since = store.silence_since()
    return (now - since).days if since else 0


def _is_stale(store: Store, now: datetime) -> bool:
    since = store.silence_since()
    return since is not None and now - since > STALE_AFTER


def _alert_outcome(cfg: Config, store: Store, alerter: Alerter,
                   result: SessionResult, now: datetime) -> None:
    if result.blocked:
        until = store.blocked_until()
        if until is not None:
            alert_blocked(alerter, result.blocked, until, cfg.tz)
    # One missing grid is routinely a slow response. Every visited profile
    # missing it is never that: it is the capture itself that broke, and it
    # deserves an alert on the first run (2026-09-26 incident).
    if result.visited and len(result.grid_missing) == len(result.visited):
        alerter.send("alert", "Capture broken",
                     f"None of the {len(result.visited)} visited profiles returned its grid. "
                     "Instagram probably changed something.")
    elif result.grid_missing:
        alerter.send("warning", "Missing grids", f"No grid for: {', '.join(result.grid_missing)}.")
    if result.failures:
        alerter.send("warning", "Partial failures",
                     f"{len(result.failures)} failure(s) on {len(result.visited)} account(s), "
                     f"{result.new_posts} new post(s). First: {result.failures[0]}")
    if _is_stale(store, now):
        alerter.send("alert", "No new posts",
                     f"Nothing new for {_silent_days(store, now)} days across all accounts. "
                     "The parser may be broken.")


def run_once(cfg: Config, store: Store, alerter: Alerter, now: datetime,
             session_factory=None) -> SessionResult:
    """One collection run, alerts included. Re-raises after alerting on a crash,
    so a one-shot caller can exit non-zero and the daemon can log and carry on."""
    # Resolved at call time, not as a default argument, so the module-level
    # Session stays the single seam tests replace.
    session_factory = session_factory or Session
    try:
        until = store.blocked_until()
        if until and until > now:
            # Chrome must not even launch during a backoff: opening a browser is
            # itself part of what a block is meant to suppress. run_session carries
            # the same check for callers that construct a session directly.
            log.warning("blocked until %s, doing nothing", until)
            _write_last_run(cfg.data_dir, mode="run", ok=True, blocked=False,
                            reason=_flat(f"backoff until {until.isoformat()}"),
                            visited=0, new_posts=0, failures=0, grid_missing=0,
                            dropped_nodes=0, stale=False, silent_days=_silent_days(store, now),
                            error="")
            return SessionResult()

        with session_factory(cfg, cfg.data_dir / "profile") as session:
            result = run_session(cfg, store, session, now)

        log.info("visited=%s new_posts=%d failures=%d grid_missing=%s dropped=%d blocked=%s "
                 "logged_out=%s", result.visited, result.new_posts, len(result.failures),
                 result.grid_missing, result.dropped_nodes, result.blocked, result.logged_out)

        if not result.blocked:
            publish(store, cfg)
        _alert_outcome(cfg, store, alerter, result, now)

        _write_last_run(
            cfg.data_dir,
            mode="run",
            ok=result.blocked is None and result.logged_out is None and not result.failures,
            blocked=result.blocked is not None,
            reason=_flat(result.blocked or result.logged_out or ""),
            visited=len(result.visited),
            accounts=_flat(",".join(result.visited)),
            new_posts=result.new_posts,
            failures=len(result.failures),
            first_failure=_flat(result.failures[0] if result.failures else ""),
            grid_missing=len(result.grid_missing),
            dropped_nodes=result.dropped_nodes,
            stale=_is_stale(store, now),
            silent_days=_silent_days(store, now),
            error="",
        )
        return result
    except Exception as exc:
        log.exception("run crashed")
        alerter.send("alert", "Run crashed", _flat(exc))
        _write_last_run(cfg.data_dir, mode="run", ok=False, blocked=False,
                        reason="", visited=0, new_posts=0, failures=1,
                        grid_missing=0, dropped_nodes=0, stale=False,
                        silent_days=0, error=_flat(exc))
        raise


def _stop_on_sigterm(signum, frame) -> None:
    # Docker stops a container with SIGTERM, whose default action kills the
    # process on the spot. Raised as SystemExit instead, it unwinds the stack:
    # Caddy, the login screen and Chrome are all shut down by their own cleanup.
    raise SystemExit(0)


def _dispatch(args, cfg: Config, alerter: Alerter) -> int:
    if args.serve:
        # Imported here: daemon imports run_once from this module.
        from .daemon import serve
        signal.signal(signal.SIGTERM, _stop_on_sigterm)
        try:
            serve(cfg)
        except KeyboardInterrupt:
            pass
        return EXIT_OK

    store = Store(cfg.data_dir)
    now = datetime.now(timezone.utc)

    if args.republish:
        log.info("republished %d feeds", len(publish(store, cfg)))
        return EXIT_OK

    # The manual modes obey the same rules as the daemon: never open Chrome
    # during a backoff or on a session known to be logged out, and never next
    # to another Chrome on the same profile.
    # --run needs no backoff check here: run_once has its own, which also
    # writes the report.
    if args.capture:
        until = store.blocked_until()
        if until and until > now:
            log.warning("blocked until %s; not opening a browser", until)
            return EXIT_BLOCKED
    if args.run or args.capture:
        if store.login_required():
            log.error("an Instagram login is pending (%s); sign in on the login screen first",
                      store.login_required())
            return EXIT_FAILED
    try:
        with session_lock(cfg.data_dir, wait=False):
            return _dispatch_locked(args, cfg, alerter, store, now)
    except SessionBusy as exc:
        print(f"busy: {exc}; try again once it is done", file=sys.stderr)
        return EXIT_FAILED


def _dispatch_locked(args, cfg: Config, alerter: Alerter, store: Store, now: datetime) -> int:
    profile_dir = cfg.data_dir / "profile"

    if args.login:
        # Forced even when nothing asked for it: a manual --login is how a
        # person refreshes a session they suspect, and run_login only opens the
        # screen for a session marked as needing it.
        if store.login_required() is None:
            store.require_login("manual login", now)
        run_login(cfg, store, alerter)
        if store.login_required() is None:
            return EXIT_OK
        until = store.blocked_until()
        return EXIT_BLOCKED if until and until > datetime.now(timezone.utc) else EXIT_FAILED

    if args.capture:
        try:
            with Session(cfg, profile_dir) as session:
                for payload in session.visit_profile(args.capture):
                    log.info("captured %s", store.archive_raw(args.capture, payload, now))
        except LoggedOutError as exc:
            # Same rule as a scheduled run: a lost session needs a login, and a
            # backoff would only delay it.
            store.require_login(str(exc), now)
            log.error("logged out: %s -- run --login or restart the container", exc)
            return EXIT_FAILED
        except BlockedError as exc:
            # A block met by hand is still a block: the backoff applies, or the
            # next scheduled run walks straight back into it.
            store.record_block(str(exc), now)
            raise
        return EXIT_OK

    result = run_once(cfg, store, alerter, now)
    return EXIT_BLOCKED if result.blocked else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(prog="instascrap")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--serve", action="store_true",
                      help="run continuously: scheduled runs, login screen, feed server")
    mode.add_argument("--run", action="store_true", help="one collection run, now")
    mode.add_argument("--login", action="store_true", help="open the login screen now")
    mode.add_argument("--capture", metavar="ACCOUNT", help="visit one profile, archive raw only")
    mode.add_argument("--republish", action="store_true", help="rebuild feeds from the archive")
    args = parser.parse_args(argv)

    # Everything past argument parsing sits inside this try. load_config, Store,
    # publish and the staleness check all used to run outside it: publish() runs
    # feedgen over arbitrary captions on every successful session and fails on a
    # full disk, which left the archive growing while the feeds silently froze.
    cfg: Config | None = None
    alerter = Alerter(None)
    try:
        # INSTASCRAP_DATA_DIR is deliberately absent from the Unraid template: in
        # the image the volume is always /data. It exists for tests and for
        # running the code outside a container.
        # `docker exec` runs as root unless told otherwise, and root-owned files
        # in the profile or archive then break the daemon, which runs as PUID.
        if not args.serve and os.geteuid() == 0 and os.environ.get("PUID", "99") != "0":
            print("refusing to run as root: files written now would be unwritable for "
                  "the daemon. Use: docker exec --user 99:100 <container> python3 -m instascrap ...",
                  file=sys.stderr)
            return EXIT_FAILED
        try:
            cfg = load_config(os.environ, Path(os.environ.get("INSTASCRAP_DATA_DIR", "/data")))
        except ConfigError as exc:
            # Read by someone filling in the Unraid form: the message names the
            # variable, a traceback would only bury it.
            print(f"configuration error: {exc}", file=sys.stderr)
            return EXIT_FAILED
        alerter = Alerter(cfg.discord_webhook)
        return _dispatch(args, cfg, alerter)
    except BlockedError as exc:
        log.error("blocked: %s", exc)
        if cfg is not None:
            until = Store(cfg.data_dir).blocked_until()
            if until is not None:
                alert_blocked(alerter, str(exc), until, cfg.tz)
            _write_last_run(cfg.data_dir, mode="cli", ok=False, blocked=True,
                            reason=_flat(exc), visited=0, new_posts=0, failures=0,
                            grid_missing=0, dropped_nodes=0, stale=False,
                            silent_days=0, error="")
        return EXIT_BLOCKED
    except Exception as exc:  # noqa: BLE001 — last line of defence
        # run_once has already alerted and reported its own crashes; this covers
        # the other modes and failures before a run starts.
        log.exception("instascrap failed")
        if cfg is not None and not args.run:
            alerter.send("alert", "Run crashed", _flat(exc))
            _write_last_run(cfg.data_dir, mode="cli", ok=False, blocked=False,
                            reason="", visited=0, new_posts=0, failures=1,
                            grid_missing=0, dropped_nodes=0, stale=False,
                            silent_days=0, error=_flat(exc))
        return EXIT_FAILED
