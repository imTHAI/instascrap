"""Archive -> RSS. Feeds are regenerated wholesale every session; nothing here is
incremental, so a partial write can never leave a half-correct feed behind."""

import os
from html import escape
from pathlib import Path
from typing import Sequence

from feedgen.feed import FeedGenerator

from .config import Config
from .models import Post
from .store import Store


def _public_url(cfg: Config, rel_path: str) -> str:
    return f"{cfg.public_base_url}/{cfg.public_prefix}/{rel_path}"


def _describe(post: Post, cfg: Config) -> str:
    parts = []
    for media in post.media:
        if media.path is None:
            continue
        url = escape(_public_url(cfg, media.path), quote=True)
        if media.kind == "video":
            # preload="none" matters: News Explorer would otherwise pull every
            # video in the list view. It also means the element paints nothing at
            # all until tapped, so the poster is what makes the entry readable.
            poster = ""
            if media.poster_path:
                poster = f' poster="{escape(_public_url(cfg, media.poster_path), quote=True)}"'
            parts.append(
                f'<video controls preload="none"{poster} width="{media.width}" src="{url}"></video>'
            )
        else:
            parts.append(f'<img src="{url}" width="{media.width}" height="{media.height}">')
    if post.caption:
        # Instagram captions are newline-separated paragraphs; without this they
        # render as one run-on block in the reader. Escaped first, so the <br>
        # inserted afterwards is the only markup that survives from a caption.
        caption = escape(post.caption).replace("\n", "<br>")
        parts.append(f"<p>{caption}</p>")
    return "".join(parts)


def _enclosure_length(cfg: Config, rel_path: str) -> int:
    """RSS wants the real byte count. A literal 0 makes some readers skip the
    enclosure outright, and tells the user nothing about download size."""
    try:
        return (cfg.data_dir / rel_path).stat().st_size
    except OSError:
        return 0


def build_feed(account: str, posts: Sequence[Post], cfg: Config,
               title: str | None = None, link: str | None = None,
               description: str | None = None) -> str:
    # `account` is the feed's slug, never its display name: it must match the file
    # actually written, since the id and the self link are built from it. Passing
    # "tous les comptes" here produced an id full of spaces pointing at a file
    # that does not exist. Human-facing wording goes in `title`/`description`.
    self_url = f"{cfg.public_base_url}/{cfg.public_prefix}/{account}.xml"
    generator = FeedGenerator()
    generator.id(self_url)
    generator.title(title or f"Instagram — {account}")
    generator.link(href=link or f"https://www.instagram.com/{account}/", rel="alternate")
    # rel="self" is what makes the id verifiable from the feed itself; RSS drops
    # the atom id, so without this the slug would be unobservable to a reader.
    generator.link(href=self_url, rel="self")
    generator.description(description or f"Posts de @{account}, réhébergés")
    generator.language("fr")

    # feedgen emits items in reverse insertion order, so insert oldest first.
    for post in sorted(posts, key=lambda p: p.timestamp_utc):
        entry = generator.add_entry()
        entry.id(post.permalink)
        entry.guid(post.permalink, permalink=True)
        entry.title(post.caption.splitlines()[0][:120] if post.caption
                    else f"@{post.account} — {post.timestamp_utc:%d/%m/%Y}")
        entry.link(href=post.permalink)
        entry.pubDate(post.timestamp_utc)
        entry.description(_describe(post, cfg))
        primary = post.media[0] if post.media else None
        if primary is not None and primary.path:
            entry.enclosure(_public_url(cfg, primary.path),
                            _enclosure_length(cfg, primary.path),
                            "video/mp4" if primary.kind == "video" else "image/jpeg")

    return generator.rss_str(pretty=True).decode()


def _write_text_atomic(path: Path, text: str) -> None:
    # Caddy serves this directory live. A reader polling mid-write would otherwise
    # get a truncated feed; everything else in the codebase already writes this way.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _hardlink(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return
    try:
        os.link(source, target)
    except OSError:
        # Falls back to a copy when data and publish sit on different filesystems.
        target.write_bytes(source.read_bytes())


def publish(store: Store, cfg: Config) -> list[Path]:
    out_dir = cfg.publish_dir / cfg.public_prefix
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    everything: list[Post] = []

    for account in cfg.accounts:
        posts = store.load_posts(account, limit=cfg.feed_items)
        everything.extend(posts)

        path = out_dir / f"{account}.xml"
        _write_text_atomic(path, build_feed(account, posts, cfg))
        written.append(path)

        for post in posts:
            for media in post.media:
                for rel in (media.path, media.poster_path):
                    if rel:
                        _hardlink(store.data_dir / rel, out_dir / rel)

    everything.sort(key=lambda p: p.timestamp_utc, reverse=True)
    all_path = out_dir / "all.xml"
    _write_text_atomic(all_path, build_feed(
        "all", everything[:cfg.feed_items], cfg,
        title="Instagram — tous les comptes",
        link="https://www.instagram.com/",
        description="Tous les comptes suivis, réhébergés",
    ))
    written.append(all_path)
    return written
