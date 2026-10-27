"""Orchestration. The rule that outranks everything else: a block ends the session
on the spot. No retry, no next account."""

import argparse
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .browser import BlockedError, LoggedOutError, Session
from .config import Config, load_config
from .feed import publish
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


def _warn_if_blocked(store: Store, now: datetime) -> None:
    """--login is reached for right after a block notification, which makes it the
    single riskiest moment to open a browser on this account: it is the same
    'insist after a refusal' that got the personal account flagged for good."""
    until = store.blocked_until()
    if not (until and until > now):
        return
    remaining = until - now
    hours = remaining.days * 24 + remaining.seconds // 3600
    banner = "!" * 72
    log.warning("\n%s\nBLOCAGE ACTIF — encore %d h (jusqu'au %s).\n"
                "Ouvrir un navigateur maintenant, c'est insister après un refus.\n"
                "Attends la fin du backoff sauf si tu sais précisément pourquoi tu passes outre.\n%s",
                banner, hours, until.isoformat(), banner)


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


def _dispatch(args, cfg: Config, alerter: Alerter) -> int:
    store = Store(cfg.data_dir)
    profile_dir = cfg.data_dir / "profile"
    now = datetime.now(timezone.utc)

    if args.republish:
        log.info("republished %d feeds", len(publish(store, cfg)))
        return EXIT_OK

    if args.login:
        _warn_if_blocked(store, now)
        # Mirrors --capture below: catch the one exception that matters,
        # record it, and let it propagate to main()'s own BlockedError
        # handler for the notification and the exit code. Needed here for a
        # reason --capture never had to consider: --login's body no longer
        # aborts on its own declared navigation (that was the bug fixed
        # immediately before this), which means it can now run all the way
        # to the operator's Enter keypress with a block sitting untouched in
        # session._block -- stashed by a background response (a
        # challenge_required on an XHR fired while the human is typing their
        # password, say) that never made anything raise. Nothing else in
        # this branch ever looks at _block again after the declared
        # navigation ends, so raise_if_blocked() below is the only thing
        # standing between that and a silent EXIT_OK -- the exact "insist
        # after a refusal" pattern this whole project exists to prevent, and
        # one the next scheduled --run would walk straight into with zero
        # backoff.
        try:
            with Session(cfg, profile_dir, headless=False) as session:
                # This navigation is expected to land on the login page -- that is
                # the whole point of --login -- so it declares that intent for its
                # own duration only. Still routed through goto(), not a raw
                # page.goto(): this morning's regression log shows the login
                # navigation itself timing out on its first attempt (the startup
                # race goto()'s retry exists for), and dropping the retry to work
                # around the block-detection bug would have traded one outage for
                # another. See Session.expect_login_redirect's docstring for why
                # this cannot leak into any other navigation.
                with session.expect_login_redirect():
                    session.goto("https://www.instagram.com/accounts/login/")
                log.info("log in manually, then press Enter here")
                input()
                # The check this whole change is for: a block stashed at any
                # point after the declared navigation above -- while the
                # human was authenticating -- surfaces here, before this
                # function ever gets to return its OK exit code.
                session.raise_if_blocked()
        except BlockedError as exc:
            store.record_block(str(exc), now)
            raise
        return EXIT_OK

    if args.capture:
        try:
            with Session(cfg, profile_dir) as session:
                for payload in session.visit_profile(args.capture):
                    log.info("captured %s", store.archive_raw(args.capture, payload, now))
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
    mode.add_argument("--run", action="store_true", help="one collection session")
    mode.add_argument("--login", action="store_true", help="headed session for manual login")
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
        cfg = load_config(os.environ, Path(os.environ.get("INSTASCRAP_DATA_DIR", "/data")))
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
