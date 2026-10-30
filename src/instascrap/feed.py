"""Archive -> RSS. Feeds are regenerated wholesale every session; nothing here is
incremental, so a partial write can never leave a half-correct feed behind."""

import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Sequence

from feedgen.feed import FeedGenerator

from .config import PREFIX_RE, Config
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
               description: str | None = None, avatar: str | None = None) -> str:
    # `account` is the feed's slug, never its display name: it must match the file
    # actually written, since the id and the self link are built from it. Passing
    # "all accounts" here produced an id full of spaces pointing at a file
    # that does not exist. Human-facing wording goes in `title`/`description`.
    self_url = f"{cfg.public_base_url}/{cfg.public_prefix}/{account}.xml"
    # The channel link points at the account page publish() writes, not at
    # instagram.com: readers that ignore <image> (NetNewsWire among them) take
    # the feed icon from the home page's <link rel="icon">, and Instagram's
    # page would give them, at best, Instagram's logo.
    home = link or _public_url(cfg, f"{account}.html")
    generator = FeedGenerator()
    generator.id(self_url)
    generator.title(title or f"Instagram — {account}")
    # rel="self" is what makes the id verifiable from the feed itself; RSS drops
    # the atom id, so without this the slug would be unobservable to a reader.
    # Added first because feedgen's RSS <link> is whichever link() call came
    # last, rel ignored: in the other order every channel linked to its own
    # .xml, and readers went looking for an icon in the feed file.
    generator.link(href=self_url, rel="self")
    generator.link(href=home, rel="alternate")
    if avatar:
        # RSS 2.0 requires the image's title and link to match the channel's.
        generator.image(url=_public_url(cfg, avatar), title=title or f"Instagram — {account}",
                        link=home)
    generator.description(description or f"Posts by @{account}, rehosted")
    generator.language("en")

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
#
# One sheet for the index and the account pages. Everything is inline: the
# pages are rewritten wholesale every run, and a separate stylesheet would be
# one more file to keep in step under a prefix that can move.
_PAGE_CSS = """
:root { color-scheme: light dark;
  --bg: #f6f5f2; --surface: #fff; --fg: #1b1a19; --muted: #6c6862; --line: #e6e3de;
  --accent: #a8481f; --ring: color-mix(in srgb, var(--accent) 45%, transparent); }
@media (prefers-color-scheme: dark) {
  :root { --bg: #131312; --surface: #1c1b1a; --fg: #ecebe8; --muted: #9a958e;
          --line: #2d2b29; --accent: #e3895c; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  -webkit-font-smoothing: antialiased; }
main { max-width: 64rem; margin: 0 auto; padding: 3rem 1.5rem 4rem; }
a { color: inherit; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }
header { margin-bottom: 2rem; }
h1 { font-size: 1.75rem; line-height: 1.2; letter-spacing: -.02em; margin: 0 0 .5rem; }
.sub { color: var(--muted); margin: 0; font-size: .9rem; }
.num { font-variant-numeric: tabular-nums; }

.all { display: flex; flex-wrap: wrap; align-items: center; gap: 1rem 1.25rem;
  background: var(--surface); border: 1px solid var(--line); border-radius: 12px;
  padding: 1rem 1.25rem; margin-bottom: 2.5rem; }
.all .text { flex: 1 1 14rem; }
.all h2 { font-size: 1rem; margin: 0; }
.stack { display: flex; padding-left: .5rem; }
.stack img, .stack span { width: 2.25rem; height: 2.25rem; margin-left: -.5rem; border-radius: 50%;
  border: 2px solid var(--surface); object-fit: cover; background: var(--line); }

.accounts { list-style: none; margin: 0; padding: 0; border-top: 1px solid var(--line); }
.account { display: grid; align-items: center; gap: .75rem 1.25rem; padding: 1.25rem 0;
  border-bottom: 1px solid var(--line);
  grid-template-columns: 3.5rem minmax(9rem, 13rem) minmax(0, 1fr) auto; }
.avatar { width: 3.5rem; height: 3.5rem; border-radius: 50%; object-fit: cover; display: block;
  background: var(--line); }
span.avatar, .stack span { display: grid; place-items: center; font-weight: 600;
  color: var(--muted); text-transform: uppercase; text-decoration: none; }
.stack span { font-size: .8rem; }
.account > a:first-child { text-decoration: none; }
.who { min-width: 0; }
.handle { font-weight: 600; text-decoration: none; overflow-wrap: anywhere; }
.handle:hover { color: var(--accent); }
.meta { color: var(--muted); font-size: .85rem; margin: .125rem 0 0; }

.thumbs { display: grid; grid-template-columns: repeat(4, 1fr); gap: .375rem; max-width: 24rem; }
.thumbs a { position: relative; display: block; aspect-ratio: 1; border-radius: 6px;
  overflow: hidden; background: var(--line); }
.thumbs img { width: 100%; height: 100%; object-fit: cover; display: block;
  transition: transform .2s ease; }
.thumbs a:hover img { transform: scale(1.04); }
.thumbs a.video::after { content: ""; position: absolute; top: .375rem; right: .375rem;
  width: .625rem; height: .75rem; background: #fff; clip-path: polygon(0 0, 100% 50%, 0 100%);
  filter: drop-shadow(0 0 2px rgb(0 0 0 / .6)); }
.waiting { color: var(--muted); font-size: .85rem; border: 1px dashed var(--line);
  border-radius: 6px; padding: .75rem 1rem; max-width: 24rem; margin: 0; }

.actions { display: flex; gap: .5rem; justify-content: flex-end; }
.btn { font: inherit; font-size: .82rem; line-height: 1; padding: .5rem .75rem; cursor: pointer;
  color: var(--fg); background: var(--surface); border: 1px solid var(--line); border-radius: 6px;
  text-decoration: none; white-space: nowrap; }
.btn:hover { border-color: var(--accent); color: var(--accent); }
.btn.copied { border-color: var(--accent); color: var(--accent); }

footer { color: var(--muted); font-size: .8rem; margin-top: 2rem; }

.profile { text-align: center; padding-top: 4rem; }
.profile .avatar { width: 7rem; height: 7rem; margin: 0 auto 1.25rem; }
.profile .actions { justify-content: center; margin-top: 1.5rem; }

@media (max-width: 44rem) {
  main { padding: 2rem 1rem 3rem; }
  .account { grid-template-columns: 3rem minmax(0, 1fr) auto; }
  .avatar { width: 3rem; height: 3rem; }
  .thumbs, .waiting { grid-column: 1 / -1; max-width: none; }
  .account .actions { grid-row: 1; grid-column: 3; }
}
@media (prefers-reduced-motion: reduce) { .thumbs img { transition: none; } }
"""

# Progressive enhancement only: without JS the copy buttons stay hidden (the
# RSS link next to them still works) and dates stay ISO. Relative dates are
# computed in the browser because the page is rebuilt only a few times a day:
# a "today" baked in at 21:00 would be wrong by breakfast.
_PAGE_JS = """
document.querySelectorAll("button.copy").forEach((button) => {
  button.hidden = false;
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(button.dataset.url);
    } catch {
      window.prompt("Copy the feed URL:", button.dataset.url);
      return;
    }
    button.textContent = "Copied";
    button.classList.add("copied");
    setTimeout(() => { button.textContent = "Copy URL"; button.classList.remove("copied"); }, 1600);
  });
});
const relative = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
const midnight = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
document.querySelectorAll("time[data-relative]").forEach((time) => {
  const days = Math.round((midnight(new Date(time.dateTime)) - midnight(new Date())) / 864e5);
  if (Math.abs(days) < 30) {
    time.title = time.textContent;
    time.textContent = relative.format(days, "day");
  }
});
"""


def _page_head(title: str, extra: str = "") -> str:
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        # Belt and braces alongside Caddy: the prefix is the only thing keeping
        # these URLs private, and a page that gets indexed hands it away.
        '<meta name="robots" content="noindex, nofollow">\n'
        f"{extra}<title>{escape(title)}</title>\n"
        f"<style>{_PAGE_CSS}</style>\n</head>\n"
    )


def _page_foot() -> str:
    return f"<script>{_PAGE_JS}</script>\n</body>\n</html>\n"


def _feed_actions(cfg: Config, slug: str) -> str:
    url = escape(_public_url(cfg, f"{slug}.xml"), quote=True)
    return (f'<div class="actions"><button type="button" class="btn copy" data-url="{url}" '
            f'aria-live="polite" hidden>Copy URL</button>'
            f'<a class="btn" href="{url}">RSS</a></div>')


def _avatar(cfg: Config, account: str, avatar: str | None, css: str = "avatar") -> str:
    if avatar:
        src = escape(_public_url(cfg, avatar), quote=True)
        return f'<img class="{css}" src="{src}" alt="" width="56" height="56" loading="lazy">'
    # The initial stands in until the first visit has brought the picture back.
    return f'<span class="{css}" aria-hidden="true">{escape(account[:1])}</span>'


def build_account_page(account: str, avatar: str | None, cfg: Config) -> str:
    """The feed's home page, which exists for its <head>: readers that ignore
    RSS <image> fetch the channel link and take the icon declared there. The
    body only has to make sense to a human who clicks "open website"."""
    name = escape(account)
    icon_tags = ""
    if avatar:
        icon = escape(_public_url(cfg, avatar), quote=True)
        # Several spellings because readers disagree on which one they read;
        # og:image is what the ones that only look at OpenGraph pick up.
        icon_tags = (f'<link rel="icon" href="{icon}" type="image/jpeg">\n'
                     f'<link rel="apple-touch-icon" href="{icon}">\n'
                     f'<meta property="og:image" content="{icon}">\n')
    return (
        _page_head(f"Instagram — {account}", icon_tags)
        + '<body>\n<main class="profile">\n' + _avatar(cfg, account, avatar) + "\n"
        + f'<h1><a class="handle" href="https://www.instagram.com/{name}/">@{name}</a></h1>\n'
        + '<p class="sub">Instagram posts, as an RSS feed.</p>\n'
        + _feed_actions(cfg, account) + "\n</main>\n"
        + _page_foot()
    )


@dataclass(frozen=True)
class AccountSummary:
    """What the recap page shows for one account, gathered by publish() from
    what it already has in hand: recounting here would mean a second pass over
    the whole archive for nothing."""
    account: str
    archived: int
    recent: tuple[Post, ...]  # newest first
    avatar: str | None

    @property
    def latest(self) -> datetime | None:
        return self.recent[0].timestamp_utc if self.recent else None


_THUMBNAILS = 4


def _thumbnails(summary: AccountSummary, cfg: Config) -> str:
    cells = []
    for post in summary.recent:
        if len(cells) == _THUMBNAILS:
            break
        first = post.media[0] if post.media else None
        # A video's own file can't be an <img>; its cover frame can.
        rel = (first.poster_path if first.kind == "video" else first.path) if first else None
        if not rel:
            continue
        label = post.caption.splitlines()[0][:100] if post.caption else \
            f"Post of {post.timestamp_utc:%Y-%m-%d}"
        css = ' class="video"' if first.kind == "video" else ""
        cells.append(
            f'<a{css} href="{escape(post.permalink, quote=True)}">'
            f'<img src="{escape(_public_url(cfg, rel), quote=True)}" alt="{escape(label, quote=True)}" '
            f'width="96" height="96" loading="lazy" decoding="async"></a>')
    if not cells:
        return '<p class="waiting">Waiting for the first visit.</p>'
    return f'<div class="thumbs">{"".join(cells)}</div>'


def _account_row(summary: AccountSummary, cfg: Config) -> str:
    name = escape(summary.account)
    if summary.latest:
        plural = "s" if summary.archived != 1 else ""
        iso = summary.latest.isoformat()
        meta = (f'<span class="num">{summary.archived:,}</span> post{plural} · latest '
                f'<time datetime="{iso}" data-relative>{summary.latest:%Y-%m-%d}</time>')
    else:
        meta = "no posts yet"
    return (
        '<li class="account">'
        f'<a href="https://www.instagram.com/{name}/" tabindex="-1" aria-hidden="true">'
        f'{_avatar(cfg, summary.account, summary.avatar)}</a>'
        f'<div class="who"><a class="handle" href="https://www.instagram.com/{name}/">@{name}</a>'
        f'<p class="meta">{meta}</p></div>'
        f"{_thumbnails(summary, cfg)}"
        f"{_feed_actions(cfg, summary.account)}</li>"
    )


def build_index(summaries: Sequence[AccountSummary], cfg: Config, generated_at: datetime) -> str:
    """The recap page: the combined feed, then one row per account, most
    recently active first so the accounts that went quiet sink to the bottom.

    An account with no posts yet still gets a row, last: a newly added account
    that never fills is exactly what this page should make obvious, and
    omitting it would hide it."""
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    ordered = sorted(summaries, key=lambda s: s.latest or epoch, reverse=True)
    archived = sum(s.archived for s in summaries)
    in_all = min(sum(len(s.recent) for s in summaries), cfg.feed_items)
    stack = "".join(_avatar(cfg, s.account, s.avatar, css="") for s in ordered[:5])
    plural = "s" if len(summaries) != 1 else ""

    return (
        _page_head("Instagram feeds") + "<body>\n<main>\n<header>\n"
        "<h1>Instagram feeds</h1>\n"
        f'<p class="sub"><span class="num">{len(summaries)}</span> account{plural} followed · '
        f'<span class="num">{archived:,}</span> posts archived. '
        "Copy a feed URL into your reader to subscribe.</p>\n</header>\n"
        f'<section class="all" aria-labelledby="all-title"><div class="stack" aria-hidden="true">'
        f"{stack}</div>"
        f'<div class="text"><h2 id="all-title">All accounts</h2>'
        f'<p class="meta">The {in_all} most recent posts, every account in one feed</p></div>'
        f"{_feed_actions(cfg, 'all')}</section>\n"
        '<ol class="accounts" aria-label="Followed accounts">\n'
        + "\n".join(_account_row(s, cfg) for s in ordered)
        + "\n</ol>\n"
        f'<footer>Updated <time datetime="{generated_at.isoformat()}">'
        f'{generated_at:%Y-%m-%d %H:%M}</time> UTC</footer>\n</main>\n'
        + _page_foot()
    )


def _remove_old_prefixes(publish_dir: Path, current: str) -> None:
    """Changing FEED_SECRET is how a leaked feed URL gets revoked, which only
    works if the old prefix stops being served. Only directories shaped like a
    prefix are touched; the media they hold are hardlinks, the originals stay
    in the archive."""
    for entry in publish_dir.iterdir():
        if entry.is_dir() and entry.name != current and PREFIX_RE.match(entry.name):
            shutil.rmtree(entry)


def publish(store: Store, cfg: Config) -> list[Path]:
    out_dir = cfg.publish_dir / cfg.public_prefix
    out_dir.mkdir(parents=True, exist_ok=True)
    _remove_old_prefixes(cfg.publish_dir, cfg.public_prefix)
    written: list[Path] = []
    everything: list[Post] = []

    summaries: list[AccountSummary] = []

    for account in cfg.accounts:
        posts = store.load_posts(account, limit=cfg.feed_items)
        everything.extend(posts)
        avatar = store.avatar_path(account)
        summaries.append(AccountSummary(account, len(store.known_shortcodes(account)),
                                        tuple(posts), avatar))

        if avatar:
            _hardlink(store.data_dir / avatar, out_dir / avatar)

        path = out_dir / f"{account}.xml"
        _write_text_atomic(path, build_feed(account, posts, cfg, avatar=avatar))
        written.append(path)

        page = out_dir / f"{account}.html"
        _write_text_atomic(page, build_account_page(account, avatar, cfg))
        written.append(page)

        for post in posts:
            for media in post.media:
                for rel in (media.path, media.poster_path):
                    if rel:
                        _hardlink(store.data_dir / rel, out_dir / rel)

    everything.sort(key=lambda p: p.timestamp_utc, reverse=True)
    all_path = out_dir / "all.xml"
    _write_text_atomic(all_path, build_feed(
        "all", everything[:cfg.feed_items], cfg,
        title="Instagram — all accounts",
        link="https://www.instagram.com/",
        description="Every followed account, rehosted",
    ))
    written.append(all_path)

    index_path = out_dir / "index.html"
    _write_text_atomic(index_path, build_index(summaries, cfg, datetime.now(timezone.utc)))
    written.append(index_path)
    return written
