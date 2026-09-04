import json
from datetime import timezone
from pathlib import Path
from instascrap.parse import extract_posts

FIXTURES = Path(__file__).parent / "fixtures"

def load(name):
    return json.loads((FIXTURES / name).read_text())

def test_extracts_three_posts_from_web_profile_info():
    posts = extract_posts(load("web_profile_info.json"), "alice")
    assert [p.shortcode for p in posts] == ["ABC123", "VID999", "CAR777"]

def test_image_post_fields():
    post = extract_posts(load("web_profile_info.json"), "alice")[0]
    assert post.account == "alice"
    assert post.caption == "Première photo"
    assert post.permalink == "https://www.instagram.com/p/ABC123/"
    assert post.timestamp_utc.tzinfo == timezone.utc
    assert post.like_count == 42 and post.comment_count == 7
    assert post.is_carousel is False
    assert len(post.media) == 1
    assert post.media[0].kind == "image"
    assert post.media[0].url == "https://cdn.test/ABC123.jpg"
    assert post.media[0].path is None

def test_video_post_uses_video_url_and_duration():
    post = extract_posts(load("web_profile_info.json"), "alice")[1]
    assert post.caption == ""
    assert post.media[0].kind == "video"
    assert post.media[0].url == "https://cdn.test/VID999.mp4"
    assert post.media[0].duration == 14.5

def test_carousel_expands_children_not_cover():
    post = extract_posts(load("web_profile_info.json"), "alice")[2]
    assert post.is_carousel is True
    assert [m.kind for m in post.media] == ["image", "video"]
    assert post.media[1].url == "https://cdn.test/CAR777_2.mp4"

def test_extracts_from_paginated_graphql_shape():
    assert [p.shortcode for p in extract_posts(load("timeline_graphql.json"), "alice")] == ["PAG001"]

def test_unrelated_payload_yields_nothing():
    assert extract_posts(load("unrelated.json"), "alice") == []

def test_garbage_payload_does_not_raise():
    assert extract_posts({"totally": ["unexpected"]}, "alice") == []
