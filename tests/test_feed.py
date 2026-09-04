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
