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


def grid_node(shortcode="GRID01", **extra):
    node = {
        "shortcode": shortcode,
        "taken_at_timestamp": 1756729800,
        "dimensions": {"width": 1080, "height": 1350},
        "edge_media_to_caption": {"edges": []},
    }
    node.update(extra)
    return {"data": {"user": {"edge_owner_to_timeline_media": {"edges": [{"node": node}]}}}}


def test_video_node_without_video_url_falls_back_to_the_cover_image():
    # The real profile grid ships is_video: true with only display_url. Dropping
    # those nodes would silently lose every single video post.
    posts = extract_posts(grid_node(is_video=True,
                                    display_url="https://cdn.test/GRID01.jpg"), "alice")
    assert len(posts) == 1
    assert posts[0].media[0].kind == "image"
    assert posts[0].media[0].url == "https://cdn.test/GRID01.jpg"


def test_video_node_with_video_url_keeps_the_cover_as_poster():
    posts = extract_posts(grid_node(is_video=True,
                                    display_url="https://cdn.test/GRID01.jpg",
                                    video_url="https://cdn.test/GRID01.mp4"), "alice")
    media = posts[0].media[0]
    assert media.kind == "video"
    assert media.url == "https://cdn.test/GRID01.mp4"
    assert media.poster_url == "https://cdn.test/GRID01.jpg"


def test_image_media_has_no_poster():
    posts = extract_posts(grid_node(display_url="https://cdn.test/GRID01.jpg"), "alice")
    assert posts[0].media[0].poster_url is None


def test_drop_count_reports_skipped_nodes():
    from instascrap.parse import extract_posts_counted
    payload = {"data": {"user": {"edge_owner_to_timeline_media": {"edges": [
        {"node": {"shortcode": "OK", "taken_at_timestamp": 1756729800,
                  "display_url": "https://cdn.test/OK.jpg",
                  "edge_media_to_caption": {"edges": []}}},
        {"node": {"shortcode": "NOURL", "taken_at_timestamp": 1756729800}},
        {"node": {"missing": "everything"}},
    ]}}}}
    posts, dropped = extract_posts_counted(payload, "alice")
    assert [p.shortcode for p in posts] == ["OK"]
    assert dropped == 2


def test_drop_count_is_zero_on_a_clean_payload():
    from instascrap.parse import extract_posts_counted
    _, dropped = extract_posts_counted(load("web_profile_info.json"), "alice")
    assert dropped == 0
