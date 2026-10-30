import gzip
import json
from datetime import datetime, timedelta, timezone
from instascrap.models import Media, Post
from instascrap.store import Store

NOW = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
BYTES = {"https://cdn.test/1.jpg": b"jpegdata", "https://cdn.test/2.mp4": b"mp4data"}

def make_post(shortcode="ABC123", ts=NOW):
    return Post(
        shortcode=shortcode, account="alice", timestamp_utc=ts,
        caption="Salut", permalink=f"https://www.instagram.com/p/{shortcode}/",
        media=(
            Media("image", "https://cdn.test/1.jpg", 1080, 1080, None, None),
            Media("video", "https://cdn.test/2.mp4", 1080, 1920, 3.0, None),
        ),
        is_carousel=True, like_count=1, comment_count=0,
    )

def test_save_post_writes_media_with_correct_extensions(tmp_path):
    saved = Store(tmp_path).save_post(make_post(), BYTES)
    assert saved.media[0].path == "media/alice/ABC123/0.jpg"
    assert saved.media[1].path == "media/alice/ABC123/1.mp4"
    assert (tmp_path / saved.media[0].path).read_bytes() == b"jpegdata"

def test_save_post_writes_json_metadata(tmp_path):
    Store(tmp_path).save_post(make_post(), BYTES)
    data = json.loads((tmp_path / "posts/alice/ABC123.json").read_text())
    assert data["shortcode"] == "ABC123"
    assert data["media"][0]["path"] == "media/alice/ABC123/0.jpg"

def test_known_shortcodes_reflects_archive(tmp_path):
    store = Store(tmp_path)
    assert store.known_shortcodes("alice") == set()
    store.save_post(make_post("AAA"), BYTES)
    store.save_post(make_post("BBB"), BYTES)
    assert store.known_shortcodes("alice") == {"AAA", "BBB"}

def test_saving_twice_is_idempotent(tmp_path):
    store = Store(tmp_path)
    store.save_post(make_post(), BYTES)
    first = (tmp_path / "media/alice/ABC123/0.jpg").stat().st_mtime_ns
    store.save_post(make_post(), BYTES)
    assert (tmp_path / "media/alice/ABC123/0.jpg").stat().st_mtime_ns == first
    assert store.known_shortcodes("alice") == {"ABC123"}

def test_load_posts_returns_newest_first(tmp_path):
    store = Store(tmp_path)
    store.save_post(make_post("OLD", NOW - timedelta(days=3)), BYTES)
    store.save_post(make_post("NEW", NOW), BYTES)
    assert [p.shortcode for p in store.load_posts("alice")] == ["NEW", "OLD"]

def test_load_posts_honours_limit(tmp_path):
    store = Store(tmp_path)
    for index, code in enumerate(("A", "B", "C")):
        store.save_post(make_post(code, NOW - timedelta(days=index)), BYTES)
    assert len(store.load_posts("alice", limit=2)) == 2

def test_load_posts_on_unknown_account_is_empty(tmp_path):
    assert Store(tmp_path).load_posts("nobody") == []

def test_archive_raw_writes_gzipped_json(tmp_path):
    path = Store(tmp_path).archive_raw("alice", {"hello": "world"}, NOW)
    assert path.suffix == ".gz"
    assert json.loads(gzip.decompress(path.read_bytes())) == {"hello": "world"}

def test_archive_raw_disambiguates_same_timestamp(tmp_path):
    store = Store(tmp_path)
    first = store.archive_raw("alice", {"page": 1}, NOW)
    second = store.archive_raw("alice", {"page": 2}, NOW)
    assert first != second
    assert json.loads(gzip.decompress(first.read_bytes())) == {"page": 1}
    assert json.loads(gzip.decompress(second.read_bytes())) == {"page": 2}

def test_archive_raw_collisions_sort_in_write_order(tmp_path):
    store = Store(tmp_path)
    written = [store.archive_raw("alice", {"page": n}, NOW) for n in (1, 2, 3)]
    on_disk = sorted(p.name for p in (tmp_path / "raw/alice").iterdir())
    assert on_disk == [p.name for p in written]


def test_video_poster_is_downloaded_alongside_the_video(tmp_path):
    post = Post(
        shortcode="VID", account="alice", timestamp_utc=NOW, caption="",
        permalink="https://www.instagram.com/p/VID/",
        media=(Media("video", "https://cdn.test/v.mp4", 1080, 1920, 5.0, None,
                     poster_url="https://cdn.test/v.jpg"),),
        is_carousel=False, like_count=0, comment_count=0,
    )
    saved = Store(tmp_path).save_post(
        post, {"https://cdn.test/v.mp4": b"mp4", "https://cdn.test/v.jpg": b"jpeg"})
    assert saved.media[0].poster_path == "media/alice/VID/0_poster.jpg"
    assert (tmp_path / saved.media[0].poster_path).read_bytes() == b"jpeg"


def test_missing_poster_bytes_do_not_lose_the_post(tmp_path):
    post = Post(
        shortcode="VID", account="alice", timestamp_utc=NOW, caption="",
        permalink="https://www.instagram.com/p/VID/",
        media=(Media("video", "https://cdn.test/v.mp4", 1080, 1920, 5.0, None,
                     poster_url="https://cdn.test/v.jpg"),),
        is_carousel=False, like_count=0, comment_count=0,
    )
    saved = Store(tmp_path).save_post(post, {"https://cdn.test/v.mp4": b"mp4"})
    assert saved.media[0].path == "media/alice/VID/0.mp4"
    assert saved.media[0].poster_path is None


def test_post_json_is_written_and_read_as_utf8(tmp_path):
    store = Store(tmp_path)
    store.save_post(dataclasses_replace_caption(make_post(), "Café — 東京 ✨"), BYTES)
    assert store.load_posts("alice")[0].caption == "Café — 東京 ✨"


def dataclasses_replace_caption(post, caption):
    import dataclasses
    return dataclasses.replace(post, caption=caption)


AVATAR = "https://cdn.test/v/123_n.jpg?oh=first&oe=1"


def test_avatar_is_absent_until_saved(tmp_path):
    store = Store(tmp_path)
    assert store.avatar_path("alice") is None
    assert not store.has_avatar("alice", AVATAR)


def test_saved_avatar_is_served_from_the_account_media_dir(tmp_path):
    store = Store(tmp_path)
    rel = store.save_avatar("alice", AVATAR, b"jpeg")
    assert rel.startswith("media/alice/avatar-") and rel.endswith(".jpg")
    assert (tmp_path / rel).read_bytes() == b"jpeg"
    assert store.avatar_path("alice") == rel


def test_avatar_is_current_across_re_signed_cdn_urls(tmp_path):
    # The CDN re-signs the same picture with a new query string on every visit;
    # only the path identifies the picture, so that alone must not cost a download.
    store = Store(tmp_path)
    store.save_avatar("alice", AVATAR, b"jpeg")
    assert store.has_avatar("alice", "https://cdn.test/v/123_n.jpg?oh=second&oe=2")
    assert not store.has_avatar("alice", "https://cdn.test/v/456_n.jpg?oh=first&oe=1")


def test_new_avatar_replaces_the_old_one_under_a_new_name(tmp_path):
    # A new name per picture is what makes readers drop their cached icon.
    store = Store(tmp_path)
    old = store.save_avatar("alice", AVATAR, b"old")
    new = store.save_avatar("alice", "https://cdn.test/v/456_n.jpg?oh=x", b"new")
    assert new != old
    assert not (tmp_path / old).exists()
    assert store.avatar_path("alice") == new
