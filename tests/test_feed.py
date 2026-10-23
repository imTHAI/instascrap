import re
from datetime import datetime, timedelta, timezone
import feedparser
from instascrap.config import Config
from instascrap.feed import build_feed, publish
from instascrap.models import Media, Post
from instascrap.store import Store

NOW = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)

def cfg_for(tmp_path, feed_items=2):
    return Config(
        accounts=("alice",), data_dir=tmp_path / "data", publish_dir=tmp_path / "pub",
        public_base_url="https://example.test", public_prefix="0123456789abcdef",
        accounts_per_session=7, feed_items=feed_items, min_gap_seconds=25, max_gap_seconds=90,
    )

def post(shortcode, kind="image", ts=NOW, caption="Légende & co"):
    ext = ".jpg" if kind == "image" else ".mp4"
    return Post(
        shortcode=shortcode, account="alice", timestamp_utc=ts, caption=caption,
        permalink=f"https://www.instagram.com/p/{shortcode}/",
        media=(Media(kind, f"https://cdn.test/{shortcode}{ext}", 1080, 1080,
                     None if kind == "image" else 5.0,
                     f"media/alice/{shortcode}/0{ext}"),),
        is_carousel=False, like_count=3, comment_count=1,
    )

def test_feed_is_valid_rss_with_one_item_per_post(tmp_path):
    parsed = feedparser.parse(build_feed("alice", [post("AAA"), post("BBB")], cfg_for(tmp_path)))
    assert parsed.bozo is False
    assert len(parsed.entries) == 2
    assert parsed.feed.title == "Instagram — alice"

def test_item_links_to_original_post(tmp_path):
    parsed = feedparser.parse(build_feed("alice", [post("AAA")], cfg_for(tmp_path)))
    assert parsed.entries[0].link == "https://www.instagram.com/p/AAA/"

def test_image_is_embedded_with_public_url(tmp_path):
    xml = build_feed("alice", [post("AAA")], cfg_for(tmp_path))
    assert "https://example.test/0123456789abcdef/media/alice/AAA/0.jpg" in xml
    assert "&lt;img" in xml or "<img" in xml

def test_video_uses_video_tag_with_preload_none(tmp_path):
    xml = build_feed("alice", [post("VID", kind="video")], cfg_for(tmp_path))
    assert "video" in xml and "preload=" in xml and "controls" in xml

def test_caption_html_is_escaped(tmp_path):
    parsed = feedparser.parse(
        build_feed("alice", [post("AAA", caption="<script>x</script>")], cfg_for(tmp_path)))
    assert "<script>x</script>" not in parsed.entries[0].description

def test_publish_writes_one_file_per_account_plus_all(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.data_dir)
    store.save_post(post("AAA"), {"https://cdn.test/AAA.jpg": b"jpeg"})
    names = {p.name for p in publish(store, cfg)}
    assert "alice.xml" in names and "all.xml" in names

def test_publish_hardlinks_media_into_public_dir(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.data_dir)
    store.save_post(post("AAA"), {"https://cdn.test/AAA.jpg": b"jpeg"})
    publish(store, cfg)
    public = cfg.publish_dir / cfg.public_prefix / "media/alice/AAA/0.jpg"
    source = cfg.data_dir / "media/alice/AAA/0.jpg"
    assert public.read_bytes() == b"jpeg"
    assert public.stat().st_ino == source.stat().st_ino

def test_publish_is_idempotent(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.data_dir)
    store.save_post(post("AAA"), {"https://cdn.test/AAA.jpg": b"jpeg"})
    publish(store, cfg)
    publish(store, cfg)  # must not raise on existing hard links

def test_feed_respects_item_limit(tmp_path):
    cfg = cfg_for(tmp_path, feed_items=2)
    store = Store(cfg.data_dir)
    for index, code in enumerate(("A", "B", "C")):
        store.save_post(post(code, ts=NOW - timedelta(days=index)),
                        {f"https://cdn.test/{code}.jpg": b"jpeg"})
    publish(store, cfg)
    parsed = feedparser.parse((cfg.publish_dir / cfg.public_prefix / "alice.xml").read_text())
    assert len(parsed.entries) == 2


def video_post(shortcode="VID", poster="media/alice/VID/0_poster.jpg"):
    return Post(
        shortcode=shortcode, account="alice", timestamp_utc=NOW, caption="",
        permalink=f"https://www.instagram.com/p/{shortcode}/",
        media=(Media("video", "https://cdn.test/v.mp4", 1080, 1920, 5.0,
                     f"media/alice/{shortcode}/0.mp4", poster_url="https://cdn.test/v.jpg",
                     poster_path=poster),),
        is_carousel=False, like_count=0, comment_count=0,
    )


def test_video_carries_a_poster_and_its_height(tmp_path):
    xml = build_feed("alice", [video_post()], cfg_for(tmp_path))
    assert "https://example.test/0123456789abcdef/media/alice/VID/0_poster.jpg" in xml
    # News Explorer only paints the poster when the player has a height.
    assert re.search(r'(<|&lt;)video [^>]*poster="[^"]+"[^>]*height="1920"', xml)


def test_video_without_a_poster_still_renders(tmp_path):
    xml = build_feed("alice", [video_post(poster=None)], cfg_for(tmp_path))
    assert "poster=" not in xml
    assert "<video" in xml or "&lt;video" in xml


def test_caption_newlines_become_line_breaks(tmp_path):
    xml = build_feed("alice", [post("AAA", caption="ligne un\nligne deux")], cfg_for(tmp_path))
    assert "&lt;br&gt;" in xml or "<br>" in xml


def test_all_feed_id_matches_the_file_it_is_written_to(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.data_dir)
    store.save_post(post("AAA"), {"https://cdn.test/AAA.jpg": b"jpeg"})
    publish(store, cfg)
    xml = (cfg.publish_dir / cfg.public_prefix / "all.xml").read_text()
    assert 'href="https://example.test/0123456789abcdef/all.xml" rel="self"' in xml
    urls = re.findall(r'(?:href|url)="([^"]*)"', xml)
    assert urls and all(" " not in url for url in urls)   # the display name never leaks into a URL
    assert feedparser.parse(xml).feed.title == "Instagram — tous les comptes"


def test_enclosure_reports_the_real_byte_length(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.data_dir)
    store.save_post(post("AAA"), {"https://cdn.test/AAA.jpg": b"jpegjpegjpeg"})
    parsed = feedparser.parse(build_feed("alice", store.load_posts("alice"), cfg))
    assert parsed.entries[0].enclosures[0]["length"] == str(len(b"jpegjpegjpeg"))


def test_feeds_are_written_atomically(tmp_path):
    # A reader polling the directory Caddy serves must never see a partial file.
    cfg = cfg_for(tmp_path)
    store = Store(cfg.data_dir)
    store.save_post(post("AAA"), {"https://cdn.test/AAA.jpg": b"jpeg"})
    publish(store, cfg)
    out = cfg.publish_dir / cfg.public_prefix
    assert not list(out.glob("*.tmp"))
    assert feedparser.parse((out / "alice.xml").read_text()).bozo is False


def test_publish_hardlinks_video_posters(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.data_dir)
    store.save_post(video_post(), {"https://cdn.test/v.mp4": b"mp4",
                                   "https://cdn.test/v.jpg": b"jpeg"})
    publish(store, cfg)
    assert (cfg.publish_dir / cfg.public_prefix / "media/alice/VID/0_poster.jpg").read_bytes() == b"jpeg"


# --- the recap page published beside the feeds ---------------------------------

def index_of(tmp_path, *, accounts, posts_by_account, feed_items=50):
    cfg = cfg_for(tmp_path, feed_items=feed_items)
    cfg = Config(accounts=accounts, data_dir=cfg.data_dir, publish_dir=cfg.publish_dir,
                 public_base_url=cfg.public_base_url, public_prefix=cfg.public_prefix,
                 accounts_per_session=cfg.accounts_per_session, feed_items=cfg.feed_items,
                 min_gap_seconds=cfg.min_gap_seconds, max_gap_seconds=cfg.max_gap_seconds)
    store = Store(cfg.data_dir)
    for account, posts in posts_by_account.items():
        for p in posts:
            store.save_post(p, {m.url: b"bytes" for m in p.media})
    publish(store, cfg)
    return (cfg.publish_dir / cfg.public_prefix / "index.html").read_text()


def test_index_lists_every_account_with_its_feed_url(tmp_path):
    html = index_of(tmp_path, accounts=("alice",),
                    posts_by_account={"alice": [post("AAA"), post("BBB")]})
    assert "https://example.test/0123456789abcdef/alice.xml" in html
    assert "https://example.test/0123456789abcdef/all.xml" in html
    assert "https://www.instagram.com/alice/" in html
    assert "2 posts" in html


def test_index_shows_an_account_that_has_no_posts_yet(tmp_path):
    # A freshly added account that never fills is precisely what this page is
    # for; dropping it from the list would hide the one thing worth noticing.
    html = index_of(tmp_path, accounts=("alice", "newcomer"),
                    posts_by_account={"alice": [post("AAA")]})
    assert "@newcomer" in html
    assert "aucun post pour l'instant" in html


def test_index_asks_not_to_be_indexed(tmp_path):
    # The prefix is the only thing keeping these URLs private, and a page that
    # lists all of them is the worst one to have crawled.
    html = index_of(tmp_path, accounts=("alice",), posts_by_account={"alice": [post("AAA")]})
    assert 'name="robots" content="noindex, nofollow"' in html
