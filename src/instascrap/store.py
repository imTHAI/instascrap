"""On-disk archive and session state.

The archive is the source of truth; feeds are a disposable projection of it.
State writes go through a temp file and a rename, because a session can be killed
mid-write when the container is stopped."""

import dataclasses
import gzip
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from .models import Post

FIRST_BACKOFF = timedelta(hours=24)
REPEAT_BACKOFF = timedelta(hours=72)

_EXT = {"image": ".jpg", "video": ".mp4"}


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data_dir / "state.json"

    def _read(self) -> dict:
        if not self.state_path.exists():
            return {"visited": {}, "block": None, "last_new_post": None}
        return json.loads(self.state_path.read_text())

    def _write(self, state: dict) -> None:
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
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
        return datetime.fromisoformat(block["until"]) if block else None

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

    # --- freshness watchdog ---------------------------------------------

    def last_new_post_at(self) -> datetime | None:
        value = self._read()["last_new_post"]
        return datetime.fromisoformat(value) if value else None

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
            stored.append(dataclasses.replace(media, path=rel))

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
        posts = [Post.from_dict(json.loads(p.read_text())) for p in directory.glob("*.json")]
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
        path = directory / f"{stem}.json.gz"
        suffix = 1
        while path.exists():
            path = directory / f"{stem}-{suffix}.json.gz"
            suffix += 1
        self._write_bytes_atomic(path, gzip.compress(json.dumps(payload).encode()))
        return path
