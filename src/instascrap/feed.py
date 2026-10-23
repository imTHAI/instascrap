"""Archive -> RSS. Feeds are regenerated wholesale every session; nothing here is
incremental, so a partial write can never leave a half-correct feed behind."""

import os
from datetime import datetime, timezone
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
            # height is just as load-bearing: without it News Explorer leaves the
            # poster out and shows a blank box. Linking an <img> to the mp4 was
            # tried instead and rejected, it opens a separate player window.
            poster = ""
            if media.poster_path:
                poster = f' poster="{escape(_public_url(cfg, media.poster_path), quote=True)}"'
            parts.append(
                f'<video controls preload="none"{poster} width="{media.width}" '
                f'height="{media.height}" src="{url}"></video>'
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


# Served from inside the secret prefix, next to the feeds themselves: the whole
# point is a page whose links can be handed straight to a reader, and a link is
# only useful if it carries the prefix. Nothing here is secret that the feeds
# beside it don't already expose.
_INDEX_CSS = """
:root { color-scheme: light dark; --fg: #1a1a1a; --dim: #6b6b6b; --bg: #fbfbfa;
        --card: #fff; --line: #e6e4e0; --accent: #7a5cff; }
@media (prefers-color-scheme: dark) {
  :root { --fg: #ececec; --dim: #9a9a9a; --bg: #191919; --card: #232323;
          --line: #333; --accent: #a892ff; }
}
* { box-sizing: border-box; }
body { margin: 0; padding: 2.5rem 1.25rem 4rem; background: var(--bg); color: var(--fg);
       font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }
main { max-width: 46rem; margin: 0 auto; }
h1 { font-size: 1.5rem; margin: 0 0 .25rem; letter-spacing: -.01em; }
.sub { color: var(--dim); margin: 0 0 2rem; font-size: .9rem; }
ul { list-style: none; margin: 0; padding: 0; }
li { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
     padding: .9rem 1.1rem; margin-bottom: .6rem; display: flex; flex-wrap: wrap;
     align-items: baseline; gap: .5rem 1rem; }
li.all { border-color: var(--accent); }
.name { font-weight: 600; flex: 1 1 12rem; }
.name a { color: inherit; text-decoration: none; border-bottom: 1px solid var(--line); }
.name a:hover { border-color: var(--accent); }
.meta { color: var(--dim); font-size: .85rem; font-variant-numeric: tabular-nums; }
.feed { flex: 1 1 100%; }
.feed a { color: var(--accent); text-decoration: none; font-size: .82rem;
          font-family: ui-monospace, SFMono-Regular, Menlo, monospace; word-break: break-all; }
.feed a:hover { text-decoration: underline; }
footer { color: var(--dim); font-size: .8rem; margin-top: 2rem; }
"""


def build_index(summaries: Sequence[tuple[str, int, datetime | None]], cfg: Config,
                generated_at: datetime) -> str:
    """The recap page: one row per account, plus the combined feed.

    `summaries` is (account, item count, most recent post) as publish() already
    has it in hand — recomputed here it would mean a second pass over the whole
    archive for nothing. An account with no posts yet still gets a row: a newly
    added account that never fills is exactly what this page should make
    obvious, and omitting it would hide it."""
    rows = []
    for account, count, latest in summaries:
        name = escape(account)
        url = escape(_public_url(cfg, f"{account}.xml"), quote=True)
        if latest:
            plural = "s" if count > 1 else ""
            meta = f"{count} post{plural} · dernier le {latest.strftime('%d/%m/%Y')}"
        else:
            meta = "aucun post pour l'instant"
        rows.append(
            f'<li><span class="name"><a href="https://www.instagram.com/{name}/">@{name}</a></span>'
            f'<span class="meta">{meta}</span>'
            f'<span class="feed"><a href="{url}">{url}</a></span></li>'
        )

    all_url = escape(_public_url(cfg, "all.xml"), quote=True)
    total = sum(count for _, count, _ in summaries)
    rows.insert(0,
        f'<li class="all"><span class="name">Tous les comptes</span>'
        f'<span class="meta">{min(total, cfg.feed_items)} post(s) les plus récents</span>'
        f'<span class="feed"><a href="{all_url}">{all_url}</a></span></li>')

    return (
        "<!doctype html>\n<html lang=\"fr\">\n<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        # Belt and braces alongside Caddy: the prefix is the only thing keeping
        # these URLs private, and a page that gets indexed hands it away.
        '<meta name="robots" content="noindex, nofollow">\n'
        "<title>Flux Instagram</title>\n"
        f"<style>{_INDEX_CSS}</style>\n</head>\n<body>\n<main>\n"
        "<h1>Flux Instagram</h1>\n"
        '<p class="sub">Comptes suivis et flux RSS correspondants. '
        "Colle une URL dans ton lecteur pour t'abonner.</p>\n"
        f"<ul>\n{chr(10).join(rows)}\n</ul>\n"
        f'<footer>Mis à jour le {generated_at.strftime("%d/%m/%Y à %H:%M")} UTC</footer>\n'
        "</main>\n</body>\n</html>\n"
    )


def publish(store: Store, cfg: Config) -> list[Path]:
    out_dir = cfg.publish_dir / cfg.public_prefix
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    everything: list[Post] = []

    summaries: list[tuple[str, int, datetime | None]] = []

    for account in cfg.accounts:
        posts = store.load_posts(account, limit=cfg.feed_items)
        everything.extend(posts)
        summaries.append((account, len(posts), posts[0].timestamp_utc if posts else None))

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

    index_path = out_dir / "index.html"
    _write_text_atomic(index_path, build_index(summaries, cfg, datetime.now(timezone.utc)))
    written.append(index_path)
    return written
