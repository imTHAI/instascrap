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
            # video in the list view.
            parts.append(f'<video controls preload="none" width="{media.width}" src="{url}"></video>')
        else:
            parts.append(f'<img src="{url}" width="{media.width}" height="{media.height}">')
    if post.caption:
        parts.append(f"<p>{escape(post.caption)}</p>")
    return "".join(parts)


def build_feed(account: str, posts: Sequence[Post], cfg: Config) -> str:
    generator = FeedGenerator()
    generator.id(f"{cfg.public_base_url}/{cfg.public_prefix}/{account}.xml")
    generator.title(f"Instagram — {account}")
    generator.link(href=f"https://www.instagram.com/{account}/", rel="alternate")
    generator.description(f"Posts de @{account}, réhébergés")
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
            entry.enclosure(_public_url(cfg, primary.path), 0,
                            "video/mp4" if primary.kind == "video" else "image/jpeg")

    return generator.rss_str(pretty=True).decode()


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
        path.write_text(build_feed(account, posts, cfg), encoding="utf-8")
        written.append(path)

        for post in posts:
            for media in post.media:
                if media.path:
                    _hardlink(store.data_dir / media.path, out_dir / media.path)

    everything.sort(key=lambda p: p.timestamp_utc, reverse=True)
    all_path = out_dir / "all.xml"
    all_path.write_text(
        build_feed("tous les comptes", everything[:cfg.feed_items], cfg), encoding="utf-8"
    )
    written.append(all_path)
    return written
