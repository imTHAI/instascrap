"""On-disk archive and session state.

The archive is the source of truth; feeds are a disposable projection of it.
State writes go through a temp file and a rename, because a session can be killed
mid-write when the container is stopped."""

import dataclasses
import gzip
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from .models import Post

log = logging.getLogger(__name__)

FIRST_BACKOFF = timedelta(hours=24)
REPEAT_BACKOFF = timedelta(hours=72)

_EXT = {"image": ".jpg", "video": ".mp4"}

# "login" is None while the Instagram session is believed valid, else
# {"reason", "at", "alerted"}. Absent from state files written before it existed,
# which therefore read as logged in -- the right default for an install whose
# session has been working all along.
_DEFAULT_STATE = {"visited": {}, "block": None, "last_new_post": None, "first_run_at": None,
                  "login": None}


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data_dir / "state.json"
        if not self.state_path.exists():
            # The staleness watchdog needs a clock to measure from. Seeded here,
            # at deployment, because last_new_post only ever gets set once a post
            # has been captured — so on a fresh install with a broken parser (the
            # most likely first failure, the fixtures being hand-written) the
            # watchdog would have stayed inert forever, which is precisely the
            # silence it exists to break.
            state = dict(_DEFAULT_STATE)
            state["first_run_at"] = datetime.now(timezone.utc).isoformat()
            self._write(state)

    def _read(self) -> dict:
        """Never raises. A corrupt state.json used to take down every subsequent
        run, turning one bad write into a permanent outage."""
        if not self.state_path.exists():
            return dict(_DEFAULT_STATE)
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            log.error("state.json unreadable (%s); starting from defaults", exc)
            self._quarantine_state()
            return dict(_DEFAULT_STATE)
        if not isinstance(state, dict):
            log.error("state.json is not an object; starting from defaults")
            self._quarantine_state()
            return dict(_DEFAULT_STATE)

        merged = {**_DEFAULT_STATE, **state}
        # Per-field repair rather than wholesale reset: a schema drift in one key
        # must not throw away the visit history or, worse, an active backoff.
        if not isinstance(merged.get("visited"), dict):
            log.warning("state.json: 'visited' has the wrong shape, reset")
            merged["visited"] = {}
        block = merged.get("block")
        if block is not None and not (isinstance(block, dict) and isinstance(block.get("until"), str)):
            log.warning("state.json: 'block' has the wrong shape, reset")
            merged["block"] = None
        login = merged.get("login")
        if login is not None and not (isinstance(login, dict) and isinstance(login.get("reason"), str)):
            log.warning("state.json: 'login' has the wrong shape, reset")
            merged["login"] = None
        for key in ("last_new_post", "first_run_at"):
            if merged.get(key) is not None and not isinstance(merged[key], str):
                merged[key] = None
        return merged

    def _quarantine_state(self) -> None:
        """Keep the bad file for forensics; it is the only record of an active
        backoff, and losing one silently would mean browsing during a block."""
        try:
            os.replace(self.state_path, self.state_path.with_suffix(".json.corrupt"))
        except OSError as exc:
            log.warning("could not set the corrupt state.json aside: %s", exc)

    def _write(self, state: dict) -> None:
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.state_path)

    # --- rotation -------------------------------------------------------

    def visit_order(self, accounts, n: int) -> list[str]:
        visited = self._read()["visited"]
        # Never-visited sorts first: the empty string precedes any ISO timestamp.
        return sorted(accounts, key=lambda a: visited.get(a, ""))[:n]

    def mark_visited(self, account: str, now: datetime) -> None:
        state = self._read()
        state["visited"][account] = now.isoformat()
        self._write(state)

    # --- blocking -------------------------------------------------------

    def blocked_until(self) -> datetime | None:
        block = self._read()["block"]
        if not block:
            return None
        try:
            return datetime.fromisoformat(block["until"])
        except (TypeError, ValueError):
            log.error("state.json: unparseable block deadline %r, ignoring", block.get("until"))
            return None

    def record_block(self, reason: str, now: datetime) -> datetime:
        state = self._read()
        backoff = REPEAT_BACKOFF if state["block"] else FIRST_BACKOFF
        until = now + backoff
        state["block"] = {"reason": reason, "at": now.isoformat(), "until": until.isoformat()}
        self._write(state)
        return until

    def clear_block(self) -> None:
        state = self._read()
        state["block"] = None
        self._write(state)

    # --- Instagram session ----------------------------------------------

    def login_required(self) -> str | None:
        login = self._read()["login"]
        return login["reason"] if login else None

    def require_login(self, reason: str, now: datetime) -> None:
        state = self._read()
        # An existing entry keeps its alert timestamp: the daily reminder must
        # not restart its clock every time the condition is re-observed.
        previous = state["login"] or {}
        state["login"] = {"reason": reason, "at": now.isoformat(),
                          "alerted": previous.get("alerted"),
                          "failure_alerted": previous.get("failure_alerted")}
        self._write(state)

    def clear_login(self) -> None:
        state = self._read()
        state["login"] = None
        self._write(state)

    # `key` separates the reminders of one lost session: "alerted" for "login
    # needed", "failure_alerted" for a login screen that will not start.
    def last_login_alert(self, key: str = "alerted") -> datetime | None:
        login = self._read()["login"]
        return _parse_iso(login.get(key)) if login else None

    def mark_login_alert(self, now: datetime, key: str = "alerted") -> None:
        state = self._read()
        if state["login"] is not None:
            state["login"][key] = now.isoformat()
            self._write(state)

    # --- freshness watchdog ---------------------------------------------

    def last_new_post_at(self) -> datetime | None:
        value = self._read()["last_new_post"]
        return _parse_iso(value)

    def silence_since(self) -> datetime | None:
        """The instant the watchdog measures its 7-day window from: the last post
        captured, or failing that the day this install first ran."""
        state = self._read()
        return _parse_iso(state["last_new_post"] or state["first_run_at"])

    def mark_new_post(self, now: datetime) -> None:
        state = self._read()
        state["last_new_post"] = now.isoformat()
        self._write(state)

    # --- archive --------------------------------------------------------

    def _posts_dir(self, account: str) -> Path:
        return self.data_dir / "posts" / account

    def known_shortcodes(self, account: str) -> set[str]:
        directory = self._posts_dir(account)
        return {p.stem for p in directory.glob("*.json")} if directory.exists() else set()

    def save_post(self, post: Post, media_bytes: dict[str, bytes]) -> Post:
        media_dir = self.data_dir / "media" / post.account / post.shortcode
        media_dir.mkdir(parents=True, exist_ok=True)

        stored = []
        for index, media in enumerate(post.media):
            ext = _EXT.get(media.kind) or Path(urlparse(media.url).path).suffix or ".bin"
            rel = f"media/{post.account}/{post.shortcode}/{index}{ext}"
            target = self.data_dir / rel
            # An existing file is never rewritten: replaying a session must leave
            # mtimes and hard links untouched.
            if not target.exists():
                self._write_bytes_atomic(target, media_bytes[media.url])

            poster_rel = None
            if media.poster_url:
                poster_rel = f"media/{post.account}/{post.shortcode}/{index}_poster.jpg"
                poster_target = self.data_dir / poster_rel
                if not poster_target.exists():
                    poster_bytes = media_bytes.get(media.poster_url)
                    if poster_bytes is None:
                        # A cover frame is a nice-to-have; the video still plays
                        # without one. Never lose the post over a missing poster.
                        poster_rel = None
                    else:
                        self._write_bytes_atomic(poster_target, poster_bytes)

            stored.append(dataclasses.replace(media, path=rel, poster_path=poster_rel))

        saved = dataclasses.replace(post, media=tuple(stored))

        self._posts_dir(post.account).mkdir(parents=True, exist_ok=True)
        json_path = self._posts_dir(post.account) / f"{post.shortcode}.json"
        if not json_path.exists():
            self._write_bytes_atomic(
                json_path, json.dumps(saved.to_dict(), indent=2, ensure_ascii=False).encode()
            )
        return saved

    def _write_bytes_atomic(self, path: Path, data: bytes) -> None:
        # A container kill mid-write must never leave a truncated file behind: a
        # truncated file would still pass the `.exists()` replay guard above and
        # so would never get repaired. Write to a sibling temp file and rename,
        # matching the pattern `_write` already uses for state.json.
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def load_posts(self, account: str, limit: int | None = None) -> list[Post]:
        directory = self._posts_dir(account)
        if not directory.exists():
            return []
        posts = [Post.from_dict(json.loads(p.read_text(encoding="utf-8")))
                 for p in directory.glob("*.json")]
        posts.sort(key=lambda p: p.timestamp_utc, reverse=True)
        return posts[:limit] if limit else posts

    def archive_raw(self, account: str, payload: dict, now: datetime) -> Path:
        directory = self.data_dir / "raw" / account
        directory.mkdir(parents=True, exist_ok=True)
        stem = now.strftime("%Y%m%dT%H%M%S%f")
        # A single profile visit can call this multiple times with the same `now`
        # (one payload per page of results): the timestamp alone is not unique,
        # so disambiguate on collision rather than silently overwriting the raw
        # archive, which is the only way to re-parse history without a browser.
        # `_` (0x5F) is used rather than `-` (0x2D) because it sorts after `.`
        # (0x2E): a bare stem must always sort before any suffixed one, and the
        # zero-padded counter keeps write order intact past nine collisions
        # (`_010` after `_009`), so a directory listing stays chronological.
        path = directory / f"{stem}.json.gz"
        suffix = 1
        while path.exists():
            path = directory / f"{stem}_{suffix:03d}.json.gz"
            suffix += 1
        self._write_bytes_atomic(path, gzip.compress(json.dumps(payload).encode()))
        return path


def _parse_iso(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        log.error("state.json: unparseable timestamp %r, ignoring", value)
        return None
