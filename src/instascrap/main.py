"""Orchestration. The rule that outranks everything else: a block ends the session
on the spot. No retry, no next account."""

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .browser import BlockedError, Session
from .config import Config, load_config
from .feed import publish
from .notify import notify
from .parse import extract_posts
from .store import Store

log = logging.getLogger(__name__)
STALE_AFTER = timedelta(days=7)


@dataclass
class SessionResult:
    visited: list[str] = field(default_factory=list)
    new_posts: int = 0
    blocked: str | None = None


def run_session(cfg: Config, store: Store, session, now: datetime) -> SessionResult:
    result = SessionResult()

    until = store.blocked_until()
    if until and until > now:
        log.warning("blocked until %s, doing nothing", until)
        return result
    if until:
        store.clear_block()

    try:
        session.warm_up()
    except BlockedError as exc:
        result.blocked = str(exc)
        store.record_block(str(exc), now)
        notify("instascrap bloqué", f"{exc} — pause de sécurité", level="alert")
        return result

    for account in store.visit_order(cfg.accounts, cfg.accounts_per_session):
        try:
            payloads = session.visit_profile(account)
        except BlockedError as exc:
            result.blocked = str(exc)
            store.record_block(str(exc), now)
            notify("instascrap bloqué", f"{exc} — pause de sécurité", level="alert")
            return result

        known = store.known_shortcodes(account)
        for payload in payloads:
            store.archive_raw(account, payload, now)
            for post in extract_posts(payload, account):
                if post.shortcode in known:
                    continue
                media_bytes = {media.url: session.fetch_media(media.url) for media in post.media}
                store.save_post(post, media_bytes)
                known.add(post.shortcode)
                result.new_posts += 1

        store.mark_visited(account, now)
        result.visited.append(account)
        session.gap()

    if result.new_posts:
        store.mark_new_post(now)
    return result


def _check_staleness(store: Store, now: datetime) -> None:
    last = store.last_new_post_at()
    if last and now - last > STALE_AFTER:
        notify("instascrap silencieux",
               f"Aucun post nouveau depuis {(now - last).days} jours — parser cassé ?")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(prog="instascrap")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--run", action="store_true", help="one collection session")
    mode.add_argument("--login", action="store_true", help="headed session for manual login")
    mode.add_argument("--capture", metavar="ACCOUNT", help="visit one profile, archive raw only")
    mode.add_argument("--republish", action="store_true", help="rebuild feeds from the archive")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    store = Store(cfg.data_dir)
    profile_dir = cfg.data_dir / "profile"
    now = datetime.now(timezone.utc)

    if args.republish:
        log.info("republished %d feeds", len(publish(store, cfg)))
        return 0

    if args.login:
        with Session(cfg, profile_dir, headless=False) as session:
            session.page.goto("https://www.instagram.com/accounts/login/")
            log.info("log in manually, then press Enter here")
            input()
        return 0

    if args.capture:
        with Session(cfg, profile_dir) as session:
            for payload in session.visit_profile(args.capture):
                log.info("captured %s", store.archive_raw(args.capture, payload, now))
        return 0

    try:
        with Session(cfg, profile_dir) as session:
            result = run_session(cfg, store, session, now)
    except Exception as exc:  # noqa: BLE001 — last line of defence before cron eats it
        log.exception("session failed")
        notify("instascrap en échec", str(exc), level="alert")
        return 1

    log.info("visited=%s new_posts=%d blocked=%s",
             result.visited, result.new_posts, result.blocked)
    if not result.blocked:
        publish(store, cfg)
    _check_staleness(store, now)
    return 0
