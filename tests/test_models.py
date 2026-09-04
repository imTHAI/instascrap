import dataclasses
from datetime import datetime, timezone
import pytest
from instascrap.models import Media, Post

def make_post():
    return Post(
        shortcode="ABC123",
        account="alice",
        timestamp_utc=datetime(2026, 9, 1, 12, 30, tzinfo=timezone.utc),
        caption="Bonjour à tous 🎬",
        permalink="https://www.instagram.com/p/ABC123/",
        media=(
            Media(kind="image", url="https://cdn.test/1.jpg", width=1080, height=1350,
                  duration=None, path=None),
            Media(kind="video", url="https://cdn.test/2.mp4", width=1080, height=1920,
                  duration=14.5, path="media/alice/ABC123/1.mp4"),
        ),
        is_carousel=True,
        like_count=42,
        comment_count=7,
    )

def test_post_roundtrips_through_dict():
    post = make_post()
    assert Post.from_dict(post.to_dict()) == post

def test_to_dict_serialises_timestamp_as_iso8601():
    assert make_post().to_dict()["timestamp_utc"] == "2026-09-01T12:30:00+00:00"

def test_media_is_immutable():
    media = make_post().media[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        media.path = "nope"
